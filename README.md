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
- Gsensor UI: `http://localhost:8001`
- Controller status API: `http://localhost:8002/api/status`
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
- Central, Gsensor, and Controller run as separate services from one shared application image.
- The Controller currently uses a safe no-op adapter: it validates parameters, experiment lifecycle commands, and growth-rate samples without producing control output or connecting to OPC UA. Set `CONTROLLER_ADAPTER=module:Class` only after a translated adapter is available.
- Central, Gsensor, and Controller share the configured `EXPERIMENT_HOST_ROOT` bind mount. Gsensor writes `gsensor_processing_state.json` inside the active experiment, while Controller writes `.controller_runtime_state.json` at the shared root so container restarts preserve the active run.
- Image revisions are identified by filename, nanosecond modification time, and file size. A camera may therefore overwrite a fixed filename and still produce a new measurement frame.
- `GSENSOR_HOUGH_DEBUG_ENABLED` defaults to `false`; normal experiments keep only the latest/final detection overlays and restart state. Enable it only for local Hough diagnosis.
- A translated Controller adapter should implement `export_state()` and `restore_state()` before restart recovery is accepted for real control. An adapter without recovery support fails closed instead of resuming with lost internal state.
