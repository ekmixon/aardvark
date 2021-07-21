import asyncio
import logging
import re
from copy import copy
from typing import Any, Dict, List

from asgiref.sync import sync_to_async
from botocore.exceptions import ClientError
from cloudaux.aws.iam import list_roles, list_users
from cloudaux.aws.sts import boto3_cached_conn
from dynaconf import Dynaconf
from swag_client import InvalidSWAGDataException
from swag_client.backend import SWAGManager
from swag_client.util import parse_swag_config_options

from aardvark.exceptions import RetrieverException
from aardvark.plugins import AardvarkPlugin
from aardvark.persistence.sqlalchemy import SQLAlchemyPersistence
from aardvark.retrievers import RetrieverPlugin
from aardvark.retrievers.access_advisor import AccessAdvisorRetriever

log = logging.getLogger("aardvark")
re_account_id = re.compile(r"\d{12}")
EMPTY_QUEUE_DELAY = 1
EMPTY_QUEUE_RETRIES = 5


class RetrieverRunner(AardvarkPlugin):
    """Scheduling and execution for data retrieval tasks."""

    retrievers: List[RetrieverPlugin]
    account_queue: asyncio.Queue
    arn_queue: asyncio.Queue
    results_queue: asyncio.Queue
    failure_queue: asyncio.Queue
    failed_arns: List[str]
    tasks: List[asyncio.Future]
    num_workers: int
    swag: SWAGManager
    swag_config: Dict[str, str]
    accounts_complete: bool
    persistence: SQLAlchemyPersistence

    def __init__(
        self,
        alternative_config: Dynaconf = None,
    ):
        super().__init__(alternative_config=alternative_config)
        self.tasks = []
        self.retrievers = []
        self.failed_arns = []
        self.num_workers = self.config.get("updater_num_threads")
        self.swag_config = self.config.get("swag")
        swag_opts = parse_swag_config_options(self.swag_config["opts"])
        self.swag = SWAGManager(**swag_opts)
        self.accounts_complete = False
        self.persistence = SQLAlchemyPersistence(alternative_config=alternative_config)

    def register_retriever(self, r: RetrieverPlugin):
        """Add a retriever instance to be called during the run process."""
        self.retrievers.append(r)

    async def _run_retrievers(self, arn: str) -> Dict[str, Any]:
        """Run retriever plugins for a given ARN.

        Retriever plugins are executed in the order in which they are registered. Each retriever
        is passed the result from the previous one, starting with a dict containing an `arn` element.

        Note: The data from all previous retriever plugins is mutable by subsequent ones.
        """
        data = {
            "arn": arn,
        }
        # Iterate through retrievers, passing the results from the previous to the next.
        for r in self.retrievers:
            try:
                data = await r.run(arn, data)
            except Exception as e:
                log.error("failed to run %s on ARN %s", r, arn)
                raise RetrieverException from e
        return data

    async def _retriever_loop(self, name: str):
        """Loop to consume from self.arn_queue and call the retriever runner function."""
        log.debug("creating %s", name)
        while True:
            log.debug("getting arn from queue")
            arn = await self.arn_queue.get()
            log.debug("%s retrieving data for %s", name, arn)
            try:
                data = await self._run_retrievers(arn)
            except Exception as e:
                log.exception("failed to run retriever on ARN %s: %s", arn, str(e))
                self.failed_arns.append(arn)
                await self.failure_queue.put(arn)
                self.arn_queue.task_done()
                continue
            # TODO: handle nested data from retrievers in persistence layer
            await self.results_queue.put(data)
            self.arn_queue.task_done()

    async def _results_loop(self, name: str):
        """Loop to consume from self.results_queue and handle results."""
        log.debug("creating %s", name)
        while True:
            data = await self.results_queue.get()
            log.debug("%s storing results for %s", name, data['arn'])
            try:
                await sync_to_async(self.persistence.store_role_data)(
                    {data["arn"]: data["access_advisor"]}
                )
            except Exception as e:
                log.exception("exception occurred in results loop: %s", str(e))
                await self.failure_queue.put(data)
            self.results_queue.task_done()

    async def _get_arns_for_account(self, account: str):
        """Retrieve ARNs for roles, users, policies, and groups in an account and add them to the ARN queue."""
        conn_details: Dict[str, str] = {
            "account_number": account,
            "assume_role": self.config.get("aws_rolename"),
            "session_name": "aardvark",
            "region": self.config.get("aws_region", "us-east-1"),
            "arn_partition": self.config.get("aws_arn_partition", "aws"),
        }
        client = await sync_to_async(boto3_cached_conn)(
            "iam", service_type="client", **conn_details
        )

        for role in await sync_to_async(list_roles)(**conn_details):
            await self.arn_queue.put(role["Arn"])

        for user in await sync_to_async(list_users)(**conn_details):
            await self.arn_queue.put(user["Arn"])

        for page in await sync_to_async(client.get_paginator("list_policies").paginate)(
            Scope="Local"
        ):
            for policy in page["Policies"]:
                await self.arn_queue.put(policy["Arn"])

        for page in await sync_to_async(client.get_paginator("list_groups").paginate)():
            for group in page["Groups"]:
                await self.arn_queue.put(group["Arn"])

    async def _arn_lookup_loop(self, name: str):
        """Loop to consume from self.account_queue to retrieve and enqueue ARNs for each account."""
        log.debug(f"creating {name}")
        while True:
            log.debug("getting account from queue")
            account = await self.account_queue.get()
            log.debug(f"{name} retrieving ARNs for {account}")
            try:
                await self._get_arns_for_account(account)
            except Exception as e:
                log.exception("exception occurred in arn lookup loop: %s", str(e))
                await self.failure_queue.put(account)
            self.account_queue.task_done()

    async def _get_swag_accounts(self) -> List[Dict]:
        """Retrieve AWS accounts from SWAG based on the SWAG options in the application configuration."""
        log.debug("getting accounts from SWAG")
        try:
            all_accounts: List[Dict] = self.swag.get_all(self.swag_config["filter"])
            swag_service = self.swag_config["service_enabled_requirement"]
            if swag_service:
                all_accounts = await sync_to_async(self.swag.get_service_enabled)(
                    swag_service, accounts_list=all_accounts
                )
            else:
                all_accounts = await sync_to_async(self.swag.get_all)(search_filter=self.swag_config["filter"])
        except (KeyError, InvalidSWAGDataException, ClientError) as e:
            log.error(
                "account names passed but SWAG not configured or unavailable: %s", str(e)
            )
            raise RetrieverException("could not retrieve SWAG data") from e

        return all_accounts

    async def _queue_all_accounts(self):
        """Add all accounts to the account queue.

        Perform a SWAG lookup and add all returned accounts to `self.account_queue`."""
        for account in await self._get_swag_accounts():
            await self.account_queue.put(account["id"])

    async def _queue_arns(self, arns: List[str]):
        """Add a list of ARNs to the ARN queue."""
        for arn in arns:
            await self.arn_queue.put(arn)

    async def _queue_accounts(self, account_names: List[str]):
        """Add requested accounts to the account queue.

        Given a list of account names and/or IDs, use SWAG to look up account numbers where needed
        and add each account number to `self.account_queue`."""
        accounts = copy(account_names)
        for account in accounts:
            if re_account_id.match(account):
                await self.account_queue.put(account)
                accounts.remove(account)

        all_accounts = await self._get_swag_accounts()

        # TODO(psanders): Consider refactoring. This could be expensive for organizations
        #  with many accounts and many aliases.
        for account in all_accounts:
            # Check if the account name matches one we want. If so, queue it and carry on.
            if account.get("name") in accounts:
                await self.account_queue.put(account["id"])
                continue
            # Now check the account's aliases to see if one matches.
            alias_key = "aliases" if account["schemaVersion"] == "2" else "alias"
            for alias in account.get(alias_key, []):
                if alias in accounts:
                    await self.account_queue.put(account["id"])
                    continue

    def cancel(self):
        """Send a cancel signal to all running workers."""
        log.info("stopping runner tasks")
        for task in self.tasks:
            task.cancel()
            log.info("task %s canceled", task)

    async def run(self, accounts: List[str] = None, arns: List[str] = None):
        """Prep account queue and kick off ARN lookup, retriever, and results workers.

        Populate ARN queue with ARNs if provided. Otherwise use SWAG to look up account
        numbers and put those in the account queue.

        After that, we start `updater.num_threads` workers for each queue. Workers will NOT
        be started for the account queue if ARNs are provided since there will be no accounts
        in the queue.
        """
        self.register_retriever(AccessAdvisorRetriever())
        log.debug("starting retriever")

        self.arn_queue = asyncio.Queue()
        self.account_queue = asyncio.Queue()
        self.results_queue = asyncio.Queue()
        self.failure_queue = asyncio.Queue()

        lookup_accounts = True
        if arns:
            await self._queue_arns(arns)
            lookup_accounts = False

        # We only need to do account lookups if ARNs were not provided.
        if lookup_accounts:
            if accounts:
                await self._queue_accounts(accounts)
            else:
                await self._queue_all_accounts()

            for i in range(self.num_workers):
                name = f"arn-lookup-worker-{i}"
                task = asyncio.create_task(self._arn_lookup_loop(name))
                self.tasks.append(task)

        for i in range(self.num_workers):
            name = f"retriever-worker-{i}"
            task = asyncio.create_task(self._retriever_loop(name))
            self.tasks.append(task)

        for i in range(self.num_workers):
            name = f"results-worker-{i}"
            task = asyncio.create_task(self._results_loop(name))
            self.tasks.append(task)

        await self.account_queue.join()
        await self.arn_queue.join()
        await self.results_queue.join()

        # Clean up our workers
        self.cancel()

        while not self.failure_queue.empty():
            failure = await self.failure_queue.get()
            log.error("failure: %s", failure)

        await asyncio.gather(*self.tasks, return_exceptions=True)