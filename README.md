# Crystallization MPC Skeleton (Python)

Minimal service skeleton for the crystallization system, focused on message-based integration between components.

## Features
- Service scaffolding for central / gsensor / controller apps
- RabbitMQ message bus integration
- InfluxDB and Grafana for telemetry storage and dashboards

## Delivery Notes
- Do not deliver the local `.venv` directory. Each recipient should create their own virtual environment.
- Workspace settings disable automatic Python environment activation in PowerShell terminals to avoid `Activate.ps1` execution-policy errors on Windows.
- Generated files such as `__pycache__`, `.pytest_cache`, and `*.egg-info` should not be included in commits or handoff packages.

## Docker Quick Start
```bash
docker compose up --build
```

Services exposed by `docker-compose.yml`:
- Central UI: `http://localhost:8000`
- RabbitMQ: `localhost:5673`
- RabbitMQ management UI: `http://localhost:15673`
- InfluxDB: `http://localhost:8087`
- Grafana: `http://localhost:3000`

## Local Python Setup
Create a fresh environment per machine instead of sharing an existing `.venv`.

### Windows PowerShell
```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

If you want to activate the environment in PowerShell, you may need to allow local scripts first:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Windows Command Prompt
```bat
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -e .
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## Notes
- Keep generated Python bytecode files out of commits and release packages.
- `.dockerignore` excludes local environments and caches from Docker build context.
- The current Docker setup starts the central UI plus infrastructure services. Controller and gsensor do not currently have standalone runtime entrypoints in this repository.
