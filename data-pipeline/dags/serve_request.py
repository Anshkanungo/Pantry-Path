"""On-demand DAG: answer one Pantry Path request (dish + ZIP + allergies + diet).

Trigger it from the UI (Trigger, then fill in the form) or the CLI:

    airflow dags trigger serve_request --conf \
      '{"dish": "quiche", "zip": "45202", "avoid": ["milk", "gluten"], "diet": "vegetarian"}'

It runs `python -m pantrypath_pipeline.request` (the same command as in the terminal)
and writes result.json to data-pipeline/results/; the file's path is the task's XCom.
Params are checked against the form's schema, then passed as environment variables,
never pasted into the shell text.
"""

from datetime import UTC, datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param
from pantrypath_common import (
    ALLERGENS,
    DIETS,
    PIPELINE_HOME,
    PIPELINE_PYTHON,
    REQUEST_COMMAND,
    default_args,
)

with DAG(
    dag_id="serve_request",
    description="One request: dish + ZIP + allergies + diet -> result.json",
    doc_md=__doc__,
    schedule=None,
    start_date=datetime(2026, 10, 19, tzinfo=UTC),
    catchup=False,
    max_active_runs=4,
    # A request is interactive: fail fast (HTTP retries happen inside the step)
    default_args=default_args(retries=0),
    tags=["pantrypath", "request"],
    params={
        "dish": Param("quiche lorraine", type="string", minLength=1, maxLength=100,
                      description="Dish to cook, in plain words"),
        "zip": Param("45202", type="string", pattern=r"^\d{5}$",
                     description="US ZIP code to search stores around"),
        "avoid": Param([], type="array", items={"type": "string", "enum": ALLERGENS},
                       uniqueItems=True, description="Allergens to avoid"),
        "diet": Param("none", type="string", enum=DIETS, description="Diet to follow"),
    },
) as dag:
    BashOperator(
        task_id="build_result",
        bash_command=REQUEST_COMMAND,
        env={
            "PIPELINE_PYTHON": PIPELINE_PYTHON,
            "PANTRYPATH_ALERT_ON_FAILURE": "0",
            "REQ_DISH": "{{ params.dish }}",
            "REQ_ZIP": "{{ params.zip }}",
            "REQ_AVOID": "{{ params.avoid | join(' ') }}",
            "REQ_DIET": "{{ params.diet }}",
            "REQ_RUN_ID": "{{ run_id }}",
        },
        append_env=True,
        cwd=str(PIPELINE_HOME),
        execution_timeout=timedelta(minutes=5),
    )
