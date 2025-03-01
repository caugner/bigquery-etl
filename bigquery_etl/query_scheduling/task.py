"""Represents a scheduled Airflow task."""

import logging
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import List, Optional, Tuple

import attr
import cattrs

from bigquery_etl.dependency import extract_table_references_without_views
from bigquery_etl.metadata.parse_metadata import Metadata
from bigquery_etl.query_scheduling.utils import (
    is_date_string,
    is_email,
    is_schedule_interval,
    is_timedelta_string,
    is_valid_dag_name,
    schedule_interval_delta,
)

AIRFLOW_TASK_TEMPLATE = "airflow_task.j2"
QUERY_FILE_RE = re.compile(
    r"^(?:.*/)?([a-zA-Z0-9_-]+)/([a-zA-Z0-9_]+)/"
    r"([a-zA-Z0-9_]+)_(v[0-9]+)/(?:query\.sql|part1\.sql|script\.sql|query\.py)$"
)
DEFAULT_DESTINATION_TABLE_STR = "use-default-destination-table"


class TaskParseException(Exception):
    """Raised when task scheduling config is invalid."""

    def __init__(self, message):
        """Throw TaskParseException."""
        message = f"""
        {message}

        Expected yaml format in metadata.yaml:
        scheduling:
            dag_name: string [required]
            depends_on_past: bool [optional]
            ... <more config parameters> ...
        """

        super(TaskParseException, self).__init__(message)


class UnscheduledTask(Exception):
    """Raised when a task is not scheduled."""

    pass


@attr.s(auto_attribs=True, frozen=True)
class TaskRef:
    """
    Representation of a reference to another task.

    The task can be defined in bigquery-etl or in telemetry-airflow.
    Uses attrs to simplify the class definition and provide validation.
    Docs: https://www.attrs.org
    """

    dag_name: str = attr.ib()
    task_id: str = attr.ib()
    execution_delta: Optional[str] = attr.ib(None)
    schedule_interval: Optional[str] = attr.ib(None)
    date_partition_offset: Optional[int] = attr.ib(None)

    @property
    def task_key(self):
        """Key to uniquely identify the task."""
        return f"{self.dag_name}.{self.task_id}"

    @execution_delta.validator
    def validate_execution_delta(self, attribute, value):
        """Check that execution_delta is in a valid timedelta format."""
        if value is not None and not is_timedelta_string(value):
            raise ValueError(
                f"Invalid timedelta definition for {attribute}: {value}."
                "Timedeltas should be specified like: 1h, 30m, 1h15m, 1d4h45m, ..."
            )

    @schedule_interval.validator
    def validate_schedule_interval(self, attribute, value):
        """Validate the schedule_interval format."""
        if value is not None and not is_schedule_interval(value):
            raise ValueError(f"Invalid schedule_interval {value}.")

    def get_execution_delta(self, schedule_interval):
        """Determine execution_delta, via schedule_interval if necessary."""
        if self.execution_delta is not None:
            return self.execution_delta
        elif self.schedule_interval is not None and schedule_interval is not None:
            execution_delta = schedule_interval_delta(
                self.schedule_interval, schedule_interval
            )
            if execution_delta != "0s":
                return execution_delta
        return None


@attr.s(auto_attribs=True, frozen=True)
class FivetranTask:
    """Representation of a Fivetran data import task."""

    task_id: str = attr.ib()


# Know tasks in telemetry-airflow, like stable table tasks
# https://github.com/mozilla/telemetry-airflow/blob/main/dags/copy_deduplicate.py
EXTERNAL_TASKS = {
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="copy_deduplicate_main_ping",
        schedule_interval="0 1 * * *",
    ): ["telemetry_stable.main_v4"],
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="bq_main_events",
        schedule_interval="0 1 * * *",
    ): ["telemetry_derived.main_events_v1"],
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="event_events",
        schedule_interval="0 1 * * *",
    ): ["telemetry_derived.event_events_v1"],
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="telemetry_derived__core_clients_first_seen__v1",
        schedule_interval="0 1 * * *",
    ): ["*.core_clients_first_seen*"],
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="baseline_clients_last_seen",
        schedule_interval="0 1 * * *",
    ): ["*.baseline_clients_last_seen*"],
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="clients_last_seen_joined",
        schedule_interval="0 1 * * *",
        date_partition_offset=-1,
    ): ["*.clients_last_seen_joined*"],
    # *_stable.* should be matched last since all
    # pattern before are downstream dependencies of
    # copy_deduplicate_all.
    TaskRef(
        dag_name="copy_deduplicate",
        task_id="copy_deduplicate_all",
        schedule_interval="0 1 * * *",
    ): ["*_stable.*"],
}


@attr.s(auto_attribs=True)
class Task:
    """
    Representation of a task scheduled in Airflow.

    Uses attrs to simplify the class definition and provide validation.
    Docs: https://www.attrs.org
    """

    dag_name: str = attr.ib()
    query_file: str
    owner: str = attr.ib()
    email: List[str] = attr.ib([])
    task_name: Optional[str] = attr.ib(None)
    project: str = attr.ib(init=False)
    dataset: str = attr.ib(init=False)
    table: str = attr.ib(init=False)
    version: str = attr.ib(init=False)
    depends_on_past: bool = attr.ib(False)
    start_date: Optional[str] = attr.ib(None)
    date_partition_parameter: Optional[str] = "submission_date"
    # number of days date partition parameter should be offset
    date_partition_offset: Optional[int] = None
    # indicate whether data should be published as JSON
    public_json: bool = attr.ib(False)
    # manually specified upstream dependencies
    depends_on: List[TaskRef] = attr.ib([])
    depends_on_fivetran: List[FivetranTask] = attr.ib([])
    # manually specified downstream depdencies
    external_downstream_tasks: List[TaskRef] = attr.ib([])
    # automatically determined upstream and downstream dependencies
    upstream_dependencies: List[TaskRef] = attr.ib([])
    downstream_dependencies: List[TaskRef] = attr.ib([])
    arguments: List[str] = attr.ib([])
    parameters: List[str] = attr.ib([])
    multipart: bool = attr.ib(False)
    query_file_path: Optional[str] = None
    priority: Optional[int] = None
    referenced_tables: Optional[List[Tuple[str, str, str]]] = attr.ib(None)
    destination_table: Optional[str] = attr.ib(default=DEFAULT_DESTINATION_TABLE_STR)
    is_python_script: bool = attr.ib(False)
    task_concurrency: Optional[int] = attr.ib(None)
    retry_delay: Optional[str] = attr.ib(None)
    retries: Optional[int] = attr.ib(None)
    email_on_retry: Optional[bool] = attr.ib(None)
    gcp_conn_id: Optional[str] = attr.ib(None)
    gke_project_id: Optional[str] = attr.ib(None)
    gke_location: Optional[str] = attr.ib(None)
    gke_cluster_name: Optional[str] = attr.ib(None)
    query_project: Optional[str] = attr.ib(None)

    @property
    def task_key(self):
        """Key to uniquely identify the task."""
        return f"{self.dag_name}.{self.task_name}"

    @owner.validator
    def validate_owner(self, attribute, value):
        """Check that owner is a valid email address."""
        if not is_email(value):
            raise ValueError(f"Invalid email for task owner: {value}.")

    @email.validator
    def validate_email(self, attribute, value):
        """Check that provided email addresses are valid."""
        if not all(map(lambda e: is_email(e), value)):
            raise ValueError(f"Invalid email in DAG email: {value}.")

    @start_date.validator
    def validate_start_date(self, attribute, value):
        """Check that start_date has YYYY-MM-DD format."""
        if value is not None and not is_date_string(value):
            raise ValueError(
                f"Invalid date definition for {attribute}: {value}."
                "Dates should be specified as YYYY-MM-DD."
            )

    @dag_name.validator
    def validate_dag_name(self, attribute, value):
        """Validate the DAG name."""
        if not is_valid_dag_name(value):
            raise ValueError(
                f"Invalid DAG name {value} for task. Name must start with 'bqetl_' "
                f"or 'private_bqetl_'."
            )

    @task_name.validator
    def validate_task_name(self, attribute, value):
        """Validate the task name."""
        if value is not None:
            if len(value) < 1 or len(value) > 62:
                raise ValueError(
                    f"Invalid task name {value}. "
                    + "The task name has to be 1 to 62 characters long."
                )

    @retry_delay.validator
    def validate_retry_delay(self, attribute, value):
        """Check that retry_delay is in a valid timedelta format."""
        if value is not None and not is_timedelta_string(value):
            raise ValueError(
                f"Invalid timedelta definition for {attribute}: {value}."
                "Timedeltas should be specified like: 1h, 30m, 1h15m, 1d4h45m, ..."
            )

    def __attrs_post_init__(self):
        """Extract information from the query file name."""
        query_file_re = re.search(QUERY_FILE_RE, self.query_file)
        if query_file_re:
            self.project = query_file_re.group(1)
            self.dataset = query_file_re.group(2)
            self.table = query_file_re.group(3)
            self.version = query_file_re.group(4)

            if self.task_name is None:
                # limiting task name to allow longer dataset names
                self.task_name = f"{self.dataset}__{self.table}__{self.version}"[-62:]
                self.validate_task_name(None, self.task_name)

            if self.destination_table == DEFAULT_DESTINATION_TABLE_STR:
                self.destination_table = f"{self.table}_{self.version}"

            if self.destination_table is None and self.query_file_path is None:
                raise ValueError(
                    "One of destination_table or query_file_path must be specified"
                )
        else:
            raise ValueError(
                "query_file must be a path with format:"
                " sql/<project>/<dataset>/<table>_<version>"
                "/(query.sql|part1.sql|script.sql|query.py)"
                f" but is {self.query_file}"
            )

    @classmethod
    def of_query(cls, query_file, metadata=None, dag_collection=None):
        """
        Create task that schedules the corresponding query in Airflow.

        Raises FileNotFoundError if not metadata file exists for query.
        If `metadata` is set, then it is used instead of the metadata.yaml
        file that might exist alongside the query file.
        """
        converter = cattrs.BaseConverter()
        if metadata is None:
            metadata = Metadata.of_query_file(query_file)

        dag_name = metadata.scheduling.get("dag_name")
        if dag_name is None:
            raise UnscheduledTask(
                f"Metadata for {query_file} does not contain scheduling information."
            )

        task_config = {"query_file": str(query_file)}
        task_config.update(metadata.scheduling)

        if len(metadata.owners) <= 0:
            raise TaskParseException(
                f"No owner specified in metadata for {query_file}."
            )

        # Airflow only allows to set one owner, so we just take the first
        task_config["owner"] = metadata.owners[0]

        # Get default email from default_args if available
        default_email = []
        if dag_collection is not None:
            dag = dag_collection.dag_by_name(dag_name)
            if dag is not None:
                default_email = dag.default_args.email
        email = task_config.get("email", default_email)
        # owners get added to the email list
        task_config["email"] = list(set(email + metadata.owners))

        # data processed in task should be published
        if metadata.is_public_json():
            task_config["public_json"] = True

        try:
            return converter.structure(task_config, cls)
        except TypeError as e:
            raise TaskParseException(
                f"Invalid scheduling information format for {query_file}: {e}"
            )

    @classmethod
    def of_multipart_query(cls, query_file, metadata=None, dag_collection=None):
        """
        Create task that schedules the corresponding multipart query in Airflow.

        Raises FileNotFoundError if not metadata file exists for query.
        If `metadata` is set, then it is used instead of the metadata.yaml
        file that might exist alongside the query file.
        """
        task = cls.of_query(query_file, metadata, dag_collection)
        task.multipart = True
        task.query_file_path = os.path.dirname(query_file)
        return task

    @classmethod
    def of_script(cls, query_file, metadata=None, dag_collection=None):
        """
        Create task that schedules the corresponding script in Airflow.

        Raises FileNotFoundError if no metadata file exists for query.
        If `metadata` is set, then it is used instead of the metadata.yaml
        file that might exist alongside the query file.
        """
        task = cls.of_query(query_file, metadata, dag_collection)
        task.query_file_path = query_file
        task.destination_table = None
        return task

    @classmethod
    def of_python_script(cls, query_file, metadata=None, dag_collection=None):
        """
        Create a task that schedules the Python script file in Airflow.

        Raises FileNotFoundError if no metadata file exists for query.
        If `metadata` is set, then it is used instead of the metadata.yaml
        file that might exist alongside the query file.
        """
        task = cls.of_query(query_file, metadata, dag_collection)
        task.query_file_path = query_file
        task.is_python_script = True
        return task

    def to_ref(self, dag_collection):
        """Return the task as `TaskRef`."""
        return TaskRef(
            dag_name=self.dag_name,
            task_id=self.task_name,
            date_partition_offset=self.date_partition_offset,
            schedule_interval=dag_collection.dag_by_name(
                self.dag_name
            ).schedule_interval,
        )

    def _get_referenced_tables(self):
        """Use zetasql to get tables the query depends on."""
        logging.info(f"Get dependencies for {self.task_key}")

        if self.is_python_script:
            # cannot do dry runs for python scripts
            return self.referenced_tables or []

        if self.referenced_tables is None:
            query_files = [Path(self.query_file)]

            if self.multipart:
                # dry_run all files if query is split into multiple parts
                query_files = Path(self.query_file_path).glob("*.sql")

            table_names = {
                tuple(table.split("."))
                for query_file in query_files
                for table in extract_table_references_without_views(query_file)
            }

            # the order of table dependencies changes between requests
            # sort to maintain same order between DAG generation runs
            self.referenced_tables = sorted(table_names)
        return self.referenced_tables

    def with_upstream_dependencies(self, dag_collection):
        """Perform a dry_run to get upstream dependencies."""
        if self.upstream_dependencies:
            return

        dependencies = []

        def _duplicate_dependency(task_ref):
            return any(
                d.task_key == task_ref.task_key for d in self.depends_on + dependencies
            )

        for table in self._get_referenced_tables():
            upstream_task = dag_collection.task_for_table(table[0], table[1], table[2])

            if upstream_task is not None:
                task_ref = upstream_task.to_ref(dag_collection)
                if upstream_task != self and not _duplicate_dependency(task_ref):
                    # Get its upstream dependencies so its date_partition_offset gets set.
                    upstream_task.with_upstream_dependencies(dag_collection)
                    task_ref = upstream_task.to_ref(dag_collection)
                    dependencies.append(task_ref)
            else:
                # see if there are some static dependencies
                for task_ref, patterns in EXTERNAL_TASKS.items():
                    if any(fnmatchcase(f"{table[1]}.{table[2]}", p) for p in patterns):
                        if not _duplicate_dependency(task_ref):
                            dependencies.append(task_ref)
                        break  # stop after the first match

        if (
            self.date_partition_parameter is not None
            and self.date_partition_offset is None
        ):
            # adjust submission_date parameter based on whether upstream tasks have
            # date partition offsets
            date_partition_offsets = [
                dependency.date_partition_offset
                for dependency in dependencies
                if dependency.date_partition_offset
            ]

            if len(date_partition_offsets) > 0:
                self.date_partition_offset = min(date_partition_offsets)
                date_partition_offset_task_keys = [
                    dependency.task_key
                    for dependency in dependencies
                    if dependency.date_partition_offset == self.date_partition_offset
                ]
                logging.info(
                    f"Set {self.task_key} date partition offset"
                    f" to {self.date_partition_offset}"
                    f" based on {', '.join(date_partition_offset_task_keys)}."
                )

        self.upstream_dependencies = dependencies

    def with_downstream_dependencies(self, dag_collection):
        """Get downstream tasks by looking up upstream dependencies in DAG collection."""
        self.downstream_dependencies = [
            task_ref
            for task_ref in dag_collection.get_task_downstream_dependencies(self)
            if task_ref.dag_name != self.dag_name
        ]
