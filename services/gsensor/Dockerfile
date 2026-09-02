# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-gsensor.txt /tmp/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --disable-pip-version-check -r /tmp/requirements-gsensor.txt
RUN python -c "from kornia.feature import LoFTR; import hashlib, pathlib, torch; LoFTR(pretrained='outdoor'); p=pathlib.Path(torch.hub.get_dir())/'checkpoints'/'loftr_outdoor.ckpt'; assert hashlib.sha256(p.read_bytes()).hexdigest() == '21f5bec5968178e8bc8b7633441836fe5de4f47d861dd2cd7dc38e271b0479ec'"

COPY src /app/src
COPY params_default.yaml params_runtime.yaml param_meta.yaml operation_meta.yaml /app/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

CMD ["uvicorn", "crystallization_mpc.apps.gsensor.app:web_app", "--host", "0.0.0.0", "--port", "8001"]
