# Crystallization MPC Skeleton (Python)

This is a **minimal, runnable skeleton** for the crystallization system services, focused on message-based integration between components.

## Features
- Service scaffolding for central / gsensor / controller apps
- RabbitMQ message bus integration (topic exchange + routing)
- InfluxDB and Grafana for telemetry storage and dashboards

## Quick Start
```bash
docker compose up --build
```

## Files
- `src/crystallization_mpc/apps/central/comm_test.py` — central message bus smoke test
- `src/crystallization_mpc/apps/gsensor/comm_test.py` — gsensor message bus smoke test
- `src/crystallization_mpc/apps/controller/comm_test.py` — controller message bus smoke test
