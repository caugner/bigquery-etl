#!/usr/bin/env python3
"""Generate and run query that splits a ping table up by probe."""
from pathlib import Path

from google.cloud import bigquery

from bigquery_etl.format_sql.formatter import reformat
from bigquery_etl.util.bigquery_id import sql_table_id

# Maximum unresolved GoogleSQL query length: 1MB
# https://cloud.google.com/bigquery/quotas#query_jobs
MAX_QUERY_SIZE = 1024*1024
STANDARD_TYPES = {
    "BOOL": "bool",
    "FLOAT64": "float64",
    "INT64": "int64",
    "STRING": "string",
    "TIMESTAMP": "timestamp",
    "ARRAY<STRUCT<key STRING, value STRING>>": "string_map",
    "ARRAY<STRUCT<key STRING, value INT64>>": "int64_map",
    "ARRAY<STRUCT<key STRING, value BOOL>>": "bool_map",
}
STATIC_PATHS = [
    ("submission_timestamp",),
    ("document_id",),
    ("client_id",),
    ("normalized_channel",),
    ("sample_id",),
]
FORCE_EXTRA_PATHS = [
    ("additional_properties",)
]


def _utf8len(string):
    return len(string.encode("utf-8"))


def _type_expr(node):
    result = node.field_type
    if result == "BOOLEAN":
        result = "BOOL"
    if result == "INTEGER":
        result = "INT64"
    if result == "FLOAT":
        result = "FLOAT64"
    if result == "RECORD":
        result = (
            "STRUCT<"
            + ", ".join(f"{field.name} {_type_expr(field)}" for field in node.fields)
            + ">"
        )
    if node.mode == "REPEATED":
        result = f"ARRAY<{result}>"
    return result

def _path_name(path):
    return ".".join(path)

def _path_select(path):
    return ".".join(f"`{p}`" for p in path)


def _select_exprs(nodes, prefix: tuple[str, ...] = ()) -> str | tuple[str, ...]:
    for node in nodes:
        path = (*prefix, node.name)
        if path in STATIC_PATHS:
            continue  # skip static paths
        if node.field_type == "RECORD" and node.mode != "REPEATED":
            yield from _select_exprs(node.fields, prefix=path)
        else:
            _t = _type_expr(node)
            if _t not in STANDARD_TYPES or path in FORCE_EXTRA_PATHS:
                yield path
            else:
                yield (
                    "("
                    + f'"{_path_name(path)}",'
                    + "("
                    + ",".join(
                        (_path_select(path) if _t == t else "NULL")
                        for t in STANDARD_TYPES
                    )
                    + "))"
                )


def _group_paths(paths):
    from itertools import groupby
    return {
        key: _group_paths([path[1:] for path in group if len(path) > 1])
        for key, group in groupby(paths, key=lambda x:x[0])
    }


def _select_as_structs(nested_paths, prefix=()):
    for key, group in nested_paths.items():
        path = *prefix, key
        if group:
            yield (
                "STRUCT("
                + ",".join(
                    _select_as_structs(group, prefix=(*prefix, key))
                ) + f") AS `{key}`"
            )
        else:
            yield f"{_path_select(path)} AS `{key}`"


def get_queries(table_name, schema):
    select_exprs = list(_select_exprs(schema))
    # query size is limited, so use the minimum number of characters for spacing
    before_select_exprs = (
        "SELECT "
        + "".join(f"{_path_select(path)}," for path in STATIC_PATHS)
        + "field_name,"
        + "value,"
        + "FROM "
        + f"`{table_name}` "
        + "CROSS JOIN "
        + "UNNEST(ARRAY<STRUCT<"
        + "field_name STRING,"
        + "value STRUCT<"
        + ",".join(
            f"`{name}` {type_expr}"
            for type_expr, name in STANDARD_TYPES.items()
        )
        + ">>>["
    )
    after_select_exprs = (
        "]) WHERE DATE(submission_timestamp) = @submission_date "
        + "AND ("
        + " OR ".join(f"value.`{name}` IS NOT NULL" for name in STANDARD_TYPES.values())
        + ")"
    )
    initial_size = _utf8len(before_select_exprs) + _utf8len(after_select_exprs)
    current_size = initial_size
    current_slice = []
    extra_paths = []
    for i, expr in enumerate(select_exprs):
        if isinstance(expr, tuple):
            extra_paths.append(expr)
            continue
        if (current_size + _utf8len(expr) + 1 >= MAX_QUERY_SIZE) or (i == len(select_exprs) - 1):
            yield before_select_exprs + ",".join(current_slice) + after_select_exprs
            current_slice = []
            current_size = initial_size
        else:
            current_size += 1  # comma between expressions
        current_slice.append(expr)
        current_size += _utf8len(expr)
    # final query collects all the paths that don't fit in a standard type
    yield (
        "SELECT "
        + "".join(f"{_path_select(path)}," for path in STATIC_PATHS)
        + "".join(f"{expr}," for expr in _select_as_structs(_group_paths(extra_paths)))
        + "FROM "
        + f"`{table_name}` "
        + "WHERE DATE(submission_timestamp) = @submission_date"
    )


def main():
    # TODO accept submission date parameter
    client = bigquery.Client()
    main = client.get_table("moz-fx-data-shared-prod.telemetry_stable.main_v4")
    *queries, extra_query = get_queries(sql_table_id(main), main.schema)
    # TODO run multiple queries and copy-union the result into place like
    # copy-deduplicate, to work within the 1MB query size limit
    for i, query in enumerate(queries):
        Path(f"main_narrow_v1.part{i}.sql").write_text(query)
    Path(f"main_extra_v1.sql").write_text(reformat(extra_query))


if __name__ == "__main__":
    main()
