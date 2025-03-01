"""Generate active users aggregates per app."""
import os
from enum import Enum
from pathlib import Path

import click
from jinja2 import Environment, FileSystemLoader

from bigquery_etl.cli.utils import use_cloud_function_option
from bigquery_etl.format_sql.formatter import reformat
from bigquery_etl.util.common import render, write_sql

THIS_PATH = Path(os.path.dirname(__file__))
TABLE_NAME = "active_users_aggregates"
DATASET_FOR_UNIONED_VIEWS = "telemetry"


class Browsers(Enum):
    """Enumeration with browser names and equivalent dataset names."""

    firefox_desktop = "Firefox Desktop"
    fenix = "Fenix"
    focus_ios = "Focus iOS"
    focus_android = "Focus Android"
    firefox_ios = "Firefox iOS"
    klar_ios = "Klar iOS"


@click.command()
@click.option(
    "--output-dir",
    "--output_dir",
    help="Output directory generated SQL is written to",
    type=click.Path(file_okay=False),
    default="sql",
)
@click.option(
    "--target-project",
    "--target_project",
    help="Google Cloud project ID",
    default="moz-fx-data-shared-prod",
)
@use_cloud_function_option
def generate(target_project, output_dir, use_cloud_function):
    """Generate per-app queries, views and metadata for active users and search counts aggregates.

    The parent folders will be created if not existing and existing files will be overwritten.
    """
    env = Environment(loader=FileSystemLoader(str(THIS_PATH / "templates")))
    mobile_query_template = env.get_template("mobile_query.sql")
    desktop_query_template = env.get_template("desktop_query.sql")
    focus_android_query_template = env.get_template("focus_android_query.sql")
    metadata_template = env.get_template("metadata.yaml")
    view_template = env.get_template("view.sql")
    focus_android_view_template = env.get_template("focus_android_view.sql")
    mobile_view_template = env.get_template("mobile_view.sql")
    output_dir = Path(output_dir) / target_project

    for browser in Browsers:
        if browser.name == "firefox_desktop":
            query_sql = reformat(
                desktop_query_template.render(
                    project_id=target_project,
                    app_value=browser.value,
                    app_name=browser.name,
                )
            )
        elif browser.name == "focus_android":
            query_sql = reformat(
                focus_android_query_template.render(
                    project_id=target_project,
                    app_value=browser.value,
                    app_name=browser.name,
                )
            )
        else:
            query_sql = reformat(
                mobile_query_template.render(
                    project_id=target_project,
                    app_value=browser.value,
                    app_name=browser.name,
                )
            )

        write_sql(
            output_dir=output_dir,
            full_table_id=f"{target_project}.{browser.name}_derived.{TABLE_NAME}_v1",
            basename="query.sql",
            sql=query_sql,
            skip_existing=False,
        )

        write_sql(
            output_dir=output_dir,
            full_table_id=f"{target_project}.{browser.name}_derived.{TABLE_NAME}_v1",
            basename="metadata.yaml",
            sql=render(
                metadata_template,
                app_value=browser.value,
                app_name=browser.name,
                format=False,
            ),
            skip_existing=False,
        )

        if browser.name == "focus_android":
            write_sql(
                output_dir=output_dir,
                full_table_id=f"{target_project}.{browser.name}.{TABLE_NAME}",
                basename="view.sql",
                sql=reformat(
                    focus_android_view_template.render(
                        project_id=target_project, app_name=browser.name
                    )
                ),
                skip_existing=False,
            )
        else:
            write_sql(
                output_dir=output_dir,
                full_table_id=f"{target_project}.{browser.name}.{TABLE_NAME}",
                basename="view.sql",
                sql=reformat(
                    view_template.render(
                        project_id=target_project, app_name=browser.name
                    )
                ),
                skip_existing=False,
            )

    write_sql(
        output_dir=output_dir,
        full_table_id=f"{target_project}.{DATASET_FOR_UNIONED_VIEWS}.{TABLE_NAME}_mobile",
        basename="view.sql",
        sql=reformat(
            mobile_view_template.render(
                project_id=target_project, dataset_id=DATASET_FOR_UNIONED_VIEWS
            )
        ),
        skip_existing=False,
    )
