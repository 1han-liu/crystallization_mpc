#!/usr/bin/env python3
"""Build the dashboard with runtime annotations without hot-changing a live mount.

Output should be an isolated provisioning directory for testing, or the live
dashboard path only during an explicitly agreed rollout window.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_dashboard():
    dashboard = json.loads((ROOT / "grafana/dashboards/crystallization-mpc.json").read_text())
    annotation = json.loads((ROOT / "grafana/runtime-configuration-annotation.json").read_text())
    annotations = dashboard.setdefault("annotations", {}).setdefault("list", [])
    annotations[:] = [item for item in annotations if item.get("name") != annotation["name"]]
    annotations.append(annotation)
    return dashboard


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink():
        raise SystemExit("Refusing a symlink output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_dashboard(), indent=2) + "\n")
