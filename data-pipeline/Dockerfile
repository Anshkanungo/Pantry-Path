# Airflow image for the Pantry Path data pipeline.
#
# Airflow and the pipeline live in separate Python environments: the pipeline's pinned
# packages (pandas 3, DuckDB, Great Expectations, DVC...) go into /opt/pipeline-venv,
# so they can never clash with Airflow's own dependencies. Tasks call that venv's python.
# The code itself is not copied in: docker-compose mounts the repo at /opt/pantrypath.
FROM apache/airflow:3.3.2-python3.12

ARG AIRFLOW_VERSION=3.3.2

USER root
# Docker Desktop shows mounted files under the container's user, and git refuses
# repos it thinks someone else owns; DVC needs to read the mounted repo's git data.
RUN git config --system --add safe.directory '*' \
    && mkdir -p /opt/pipeline-venv \
    && chown airflow:0 /opt/pipeline-venv \
    && chmod 775 /opt/pipeline-venv

USER airflow
# pytest in Airflow's environment, for the DAG tests (pinning Airflow keeps pip from
# changing it)
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" "pytest==9.1.1"

COPY --chown=airflow:0 requirements.txt /tmp/pipeline-requirements.txt
RUN uv venv /opt/pipeline-venv --python /usr/python/bin/python3.12 \
    && uv pip install --no-cache --python /opt/pipeline-venv/bin/python \
        -r /tmp/pipeline-requirements.txt \
    && printf '%s\n' /opt/pantrypath/shared /opt/pantrypath/data-pipeline/src \
        > /opt/pipeline-venv/lib/python3.12/site-packages/pantrypath.pth \
    && chmod -R g+rX /opt/pipeline-venv

ENV PIPELINE_PYTHON=/opt/pipeline-venv/bin/python \
    PANTRYPATH_HOME=/opt/pantrypath/data-pipeline
