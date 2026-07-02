FROM python:3.11-slim

WORKDIR /app

COPY . /app
RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app/src
# Default entrypoint for local runs; docker-compose overrides per-service.
CMD ["uvicorn", "crystallization_mpc.apps.central.ui.app:web_app", "--host", "0.0.0.0", "--port", "8000"]
