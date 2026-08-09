# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

WORKDIR /app

# Dependencies change much less often than application code. Keeping them in a
# separate layer lets normal Python/UI edits reuse the installed dependency
# layer. The BuildKit cache also avoids downloading large wheels again when the
# dependency list itself changes.
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --disable-pip-version-check -r /tmp/requirements.txt

# Only runtime files belong in the application image. Tests, documentation,
# local experiments, and MATLAB reference material stay on the host.
COPY src /app/src
COPY params_default.yaml params_runtime.yaml param_meta.yaml operation_meta.yaml /app/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Default entrypoint for local runs; docker-compose overrides per-service.
CMD ["uvicorn", "crystallization_mpc.apps.central.ui.app:web_app", "--host", "0.0.0.0", "--port", "8000"]
