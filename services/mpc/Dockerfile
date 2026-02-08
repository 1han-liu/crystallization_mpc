FROM python:3.11-slim

WORKDIR /app

COPY services/mpc/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONPATH=/app/src
# Default entrypoint for local runs; docker-compose overrides per-service.
CMD ["python", "-m", "crystallization_mpc.apps.central.comm_test"]
