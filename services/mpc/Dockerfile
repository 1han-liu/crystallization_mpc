FROM python:3.11-slim

WORKDIR /app

COPY services/mpc/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONPATH=/app/src
# Default entrypoint for local runs; docker-compose overrides per-service.
CMD ["uvicorn", "crystallization_mpc.apps.central.ui.app:web_app", "--host", "0.0.0.0", "--port", "8000"]
