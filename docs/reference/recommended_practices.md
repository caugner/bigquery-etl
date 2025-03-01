# Recommended practices

## Queries

- Should be defined in files named as `sql/<project>/<dataset>/<table>_<version>/query.sql` e.g.
  - `<project>` defines both where the destination table resides and in which project the query job runs
    `sql/moz-fx-data-shared-prod/telemetry_derived/clients_daily_v7/query.sql`
  - Queries that populate tables should always be named with a version suffix;
    we assume that future optimizations to the data representation may require
    schema-incompatible changes such as dropping columns
- May be generated using a python script that prints the query to stdout
  - Should save output as `sql/<project>/<dataset>/<table>_<version>/query.sql` as above
  - Should be named as `sql/<project>/query_type.sql.py` e.g. `sql/moz-fx-data-shared-prod/clients_daily.sql.py`
  - May use options to generate queries for different destination tables e.g.
    using `--source telemetry_core_parquet_v3` to generate
    `sql/moz-fx-data-shared-prod/telemetry/core_clients_daily_v1/query.sql` and using `--source main_summary_v4` to
    generate `sql/moz-fx-data-shared-prod/telemetry/clients_daily_v7/query.sql`
  - Should output a header indicating options used e.g.
    ```sql
    -- Query generated by: sql/moz-fx-data-shared-prod/clients_daily.sql.py --source telemetry_core_parquet
    ```
- Should not specify a project or dataset in table names to simplify testing
- Should be [incremental](./incremental.md)
- Should filter input tables on partition and clustering columns
- Should use `_` prefix in generated column names not meant for output
- Should use `_bits` suffix for any integer column that represents a bit pattern
- Should not use `DATETIME` type, due to incompatibility with
  [spark-bigquery-connector]
- Should read from `*_stable` tables instead of including custom deduplication
  - Should use the earliest row for each `document_id` by `submission_timestamp`
    where filtering duplicates is necessary
- Should not refer to views in the `mozdata` project which are duplicates of views in another project
  (commonly `moz-fx-data-shared-prod`). Refer to the original view instead.
- Should escape identifiers that match keywords, even if they aren't [reserved keywords]

[spark-bigquery-connector]: https://github.com/GoogleCloudPlatform/spark-bigquery-connector/issues/5
[reserved keywords]: https://cloud.google.com/bigquery/docs/reference/standard-sql/lexical#reserved-keywords

## Query Metadata

- For each query, a `metadata.yaml` file should be created in the same directory
- This file contains a description, owners and labels. As an example:

```yaml
friendly_name: SSL Ratios
description: >
  Percentages of page loads Firefox users have performed that were
  conducted over SSL broken down by country.
owners:
  - example@mozilla.com
labels:
  application: firefox
  incremental: true # incremental queries add data to existing tables
  schedule: daily # scheduled in Airflow to run daily
  public_json: true
  public_bigquery: true
  review_bugs:
    - 1414839 # Bugzilla bug ID of data review
  incremental_export: false # non-incremental JSON export writes all data to a single location
```

- only labels where value types are eithers integers or strings are published, all other values types are being skipped

## Views

- Should be defined in files named as `sql/<project>/<dataset>/<table>/view.sql` e.g.
  `sql/moz-fx-data-shared-prod/telemetry/core/view.sql`
  - Views should generally _not_ be named with a version suffix; a view represents a
    stable interface for users and whenever possible should maintain compatibility
    with existing queries; if the view logic cannot be adapted to changes in underlying
    tables, breaking changes must be communicated to `fx-data-dev@mozilla.org`
- Must specify project and dataset in all table names
  - Should default to using the `moz-fx-data-shared-prod` project;
    the `scripts/publish_views` tooling can handle parsing the definitions to publish
    to other projects such as `derived-datasets`
- Should not refer to views in the `mozdata` project which are duplicates of views in another project
  (commonly `moz-fx-data-shared-prod`). Refer to the original view instead.

## UDFs

- Should limit the number of [expression subqueries] to avoid: `BigQuery error in query operation: Resources exceeded during query execution: Not enough resources for query planning - too many subqueries or query is too complex.`
- Should be used to avoid code duplication
- Must be named in files with lower snake case names ending in `.sql`
  e.g. `mode_last.sql`
  - Each file must only define effectively private helper functions and one
    public function which must be defined last
    - Helper functions must not conflict with function names in other files
  - SQL UDFs must be defined in the `udf/` directory and JS UDFs must be defined
    in the `udf_js` directory
    - The `udf_legacy/` directory is an exception which must only contain
      compatibility functions for queries migrated from Athena/Presto.
- Functions must be defined as [persistent UDFs](https://cloud.google.com/bigquery/docs/reference/standard-sql/user-defined-functions#temporary-udf-syntax)
  using `CREATE OR REPLACE FUNCTION` syntax
  - Function names must be prefixed with a dataset of `<dir_name>.` so, for example,
    all functions in `udf/*.sql` are part of the `udf` dataset
    - The final syntax for creating a function in a file will look like
      `CREATE OR REPLACE FUNCTION <dir_name>.<file_name>`
  - We provide tooling in `scripts/publish_persistent_udfs` for
    publishing these UDFs to BigQuery
    - Changes made to UDFs need to be published manually in order for the
      dry run CI task to pass
- Should use `SQL` over `js` for performance

[expression subqueries]: https://cloud.google.com/bigquery/docs/reference/standard-sql/expression_subqueries

## Backfills

- Should be documented and reviewed by a peer using a
  [new bug](https://bugzilla.mozilla.org/enter_bug.cgi) that describes
  the context that required the backfill and the command or script used.
- Should be avoided on large tables
  - Backfills may double storage cost for a table for 90 days by moving
    data from long-term storage to short-term storage
    - For example regenerating `clients_last_seen_v1` from scratch would cost
      about $1600 for the query and about $6800 for data moved to short-term
      storage
  - Should combine multiple backfills happening around the same time
  - Should delay column deletes until the next other backfill
    - Should use `NULL` for new data and `EXCEPT` to exclude from views until
      dropped
- Should use copy operations in append mode to change column order
  - Copy operations do not allow changing partitioning, changing clustering, or
    column deletes
- Should split backfilling into queries that finish in minutes not hours
- May use [script/generate_incremental_table] to automate backfilling incremental
  queries
- May be performed in a single query for smaller tables that do not depend on history
  - A useful pattern is to have the only reference to `@submission_date` be a
    clause `WHERE (@submission_date IS NULL OR @submission_date = submission_date)`
    which allows recreating all dates by passing `--parameter=submission_date:DATE:NULL`
- After running the backfill, is important to validate that the job ran without errors
  and the execution times and bytes processed are as expected.
  Errors normally appear in the parent job and may or may not include the dataset and
  table names, therefore it is important to check for errors in the jobs ran on that date.
  Here is a query you may use for this purpose:
  ```sql
  SELECT
    job_id,
    user_email,
    parent_job_id,
    creation_time,
    destination_table.dataset_id,
    destination_table.table_id,
    end_time-start_time as task_duration,
    total_bytes_processed/(1024*1024*1024) as gigabytes_processed,
    state,
    error_result.location AS error_location,
    error_result.reason AS error_reason,
    error_result.message AS error_message,
  FROM `moz-fx-data-shared-prod`.`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE DATE(creation_time) = <'YYYY-MM-DD'>
    AND user_email = <'user@mozilla.com'>
  ORDER BY creation_time DESC
  ```
