import datetime
import time
import boto3
import os
import sys
import json

import botocore.session
import shortuuid
import logging

from data_mesh_util.lib.ApiAutomator import ApiAutomator
from data_mesh_util.lib.SubscriberTracker import *


class DataMeshConsumer:
    _current_account = None
    _data_mesh_account_id = None
    _data_consumer_role_arn = None
    _data_consumer_account_id = None
    _data_mesh_manager_role_arn = None
    _session = None
    _sts_client = None
    _config = {}
    _current_region = None
    _log_level = None
    _logger = logging.getLogger("DataMeshConsumer")
    _logger.addHandler(logging.StreamHandler(sys.stdout))
    _subscription_tracker = None
    _consumer_automator = None
    _mesh_automator = None
    _ro_session = None
    _ro_credentials = None

    def __init__(self, data_mesh_account_id: str, region_name: str = 'us-east-1', log_level: str = "INFO",
                 use_credentials=None):
        if region_name is None:
            raise Exception("Cannot initialize a Data Mesh Consumer without an AWS Region")
        else:
            self._current_region = region_name

        self._data_mesh_account_id = data_mesh_account_id

        # Assume the consumer account DataMeshConsumer role, unless we have been supplied temporary credentials for that role
        self._session, _consumer_credentials, _consumer_arn = utils.assume_iam_role(
            role_name=DATA_MESH_CONSUMER_ROLENAME,
            region_name=self._current_region,
            use_credentials=use_credentials)

        self._sts_client = self._session.client('sts')

        self._log_level = log_level
        self._logger.setLevel(log_level)

        self._current_account = self._sts_client.get_caller_identity()
        self._data_consumer_account_id = self._current_account.get('Account')

        self._consumer_automator = ApiAutomator(target_account=self._data_consumer_account_id,
                                                session=self._session, log_level=self._log_level)

        # assume the DataMeshConsumer-<account-id> role in the mesh
        _data_mesh_session, _data_mesh_credentials, _data_mesh_arn = utils.assume_iam_role(
            role_name=utils.get_central_role_name(self._data_consumer_account_id, CONSUMER),
            region_name=self._current_region,
            use_credentials=_consumer_credentials,
            target_account=self._data_mesh_account_id
        )
        self._logger.debug("Created new STS Session for Data Mesh Consumer")

        utils.validate_correct_account(_data_mesh_credentials, data_mesh_account_id)

        # create the subscription tracker
        self._subscription_tracker = SubscriberTracker(credentials=_data_mesh_credentials,
                                                       data_mesh_account_id=data_mesh_account_id,
                                                       region_name=self._current_region,
                                                       log_level=self._log_level)

        # finally, generate a read-only set of credentials in the mesh
        self._ro_session, _ro_creds, _ro_arn = utils.assume_iam_role(
            role_name=DATA_MESH_READONLY_ROLENAME,
            region_name=self._current_region,
            use_credentials=_data_mesh_credentials,
            target_account=self._data_mesh_account_id
        )
        self._logger.debug("Created new STS Session for Data Mesh Read Only")

        self._mesh_automator = ApiAutomator(target_account=self._data_mesh_account_id,
                                            session=self._ro_session, log_level=self._log_level)

    def request_access_to_product(self, owner_account_id: str, database_name: str,
                                  tables: list, request_permissions: list) -> dict:
        '''
        Requests access to a specific data product from the data mesh. Request can be for an entire database, a specific
        table, but is restricted to a single principal. If no principal is provided, grants will be applied to the requesting
        consumer role only. Returns an access request ID which will be approved or denied by the data product owner
        :param database_name:
        :param table_name:
        :param requesting_principal:
        :param request_permissions:
        :return:
        '''
        table_list = utils.ensure_list(tables)
        perm_list = utils.ensure_list(request_permissions)

        # add a std out message if we are asking for an empty list of tables
        if table_list == []:
            print("Creating Database level Access Request")

        # validate that the object is visible to the consumer
        for t in table_list:
            self._mesh_automator.describe_table(database_name=database_name, table_name=t)

        return self._subscription_tracker.create_subscription_request(
            owner_account_id=owner_account_id,
            database_name=database_name,
            tables=table_list,
            principal=self._current_account.get('Account'),
            request_grants=perm_list,
            suppress_object_validation=True
        )

    def finalize_subscription(self, subscription_id: str) -> None:
        '''
        Finalizes the process of requesting access to a data product. This imports the granted subscription into the consumer's account
        :param subscription_id:
        :return:
        '''
        # grab the subscription
        subscription = self._subscription_tracker.get_subscription(subscription_id=subscription_id)
        data_mesh_database_name = subscription.get(DATABASE_NAME)

        # create a shared database reference
        self._consumer_automator.get_or_create_database(
            database_name=data_mesh_database_name,
            database_desc=f"Database to contain objects from Producer Database {subscription.get(OWNER_PRINCIPAL)}.{subscription.get(DATABASE_NAME)}",
            source_account=self._data_mesh_account_id
        )

        shares = []
        for k, v in subscription.get(RAM_SHARES).items():
            shares.append(v.get('arn'))

        # accept the RAM shares attached to the subscription
        accepted, active, not_found = self._consumer_automator.accept_lf_resource_shares(
            share_list=shares)

        if len(accepted) > 0:
            self._logger.info(f"Accepted {len(accepted)} RAM Shares: {str(accepted)}")

        if len(active) > 0:
            self._logger.info(
                f"{len(active)} RAM Shares already in Active state")

        if len(not_found) > 0:
            self._logger.warning(
                f"Unable to resolve {len(not_found)} RAM Shares: {str(not_found)}")

        # mark the subscription as finalized
        self._subscription_tracker.mark_subscription_as_imported(
            subscription_id=subscription_id
        )
        self._logger.info(f"Subscription Import Complete")

    def get_subscription(self, request_id: str) -> dict:
        return self._subscription_tracker.get_subscription(subscription_id=request_id)

    def get_table_info(self, database_name: str, table_name: str):
        return self._consumer_automator.describe_table(database_name, table_name)

    def list_product_access(self) -> dict:
        '''
        Lists active and pending product access grants.
        :return:
        '''
        me = self._sts_client.get_caller_identity().get('Account')
        return self._subscription_tracker.list_subscriptions(principal_id=me, request_status=STATUS_ACTIVE)

    def delete_subscription(self, subscription_id: str, reason: str):
        '''
        Soft delete a subscription
        :param subscription_id:
        :param reason:
        :return:
        '''
        subscription = self._subscription_tracker.get_subscription(subscription_id=subscription_id)

        # confirm that we are calling from the same account as the subscriber principal
        if subscription.get(SUBSCRIBER_PRINCIPAL) != self._current_account.get('Account'):
            raise Exception("Cannot delete permissions which you do not own")
        else:
            # leave the ram shares
            self._consumer_automator.leave_ram_shares(principal=subscription.get(SUBSCRIBER_PRINCIPAL),
                                                      ram_shares=subscription.get(RAM_SHARES))

            return self._subscription_tracker.delete_subscription(subscription_id=subscription_id, reason=reason)
