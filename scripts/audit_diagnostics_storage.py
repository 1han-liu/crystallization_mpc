#!/usr/bin/env python3
"""Query native acceptance points and all dashboard panels with a read-only token.

No database writes, dashboard mutations, or credential output are performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from crystallization_mpc.infra.influxdb.client import InfluxSettings, create_influx_client
from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env

ROOT = Path(__file__).resolve().parents[1]


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def audit(directory: Path) -> dict:
    report = json.loads((directory / "report.json").read_text())
    rows = [json.loads(line) for line in (directory / "ticks.jsonl").read_text().splitlines()]
    config = load_telemetry_env(ROOT / ".runtime/grafana-local/controller.env")
    token = json.loads((ROOT / ".runtime/grafana-local/credentials.json").read_text())["read_token"]
    settings = InfluxSettings(url=config["CONTROLLER_INFLUX_URL"], token=token,
                             org=config["CONTROLLER_INFLUX_ORG"], bucket=config["CONTROLLER_INFLUX_BUCKET"])
    start = datetime.fromisoformat(rows[0]["computed_at"].replace("Z", "+00:00")) - timedelta(seconds=1)
    stop = datetime.fromisoformat(rows[-1]["computed_at"].replace("Z", "+00:00")) + timedelta(seconds=1)
    bounds = f'|> range(start: {start.isoformat().replace("+00:00", "Z")}, stop: {stop.isoformat().replace("+00:00", "Z")})'
    base = f'from(bucket: {json.dumps(settings.bucket)}) {bounds} |> filter(fn: (r) => r._measurement == "controller_measurement" and r.run_id == {json.dumps(report["run_id"])})'
    dashboard = json.loads((ROOT / "grafana/dashboards/crystallization-mpc.json").read_text())
    checks = []
    with create_influx_client(settings) as client:
        api = client.query_api()

        def records(query):
            return [record.values for table in api.query(query, org=settings.org) for record in table.records]

        ticks = records(base + ' |> filter(fn: (r) => r._field == "tick_seq")')
        capture = records(base + ' |> filter(fn: (r) => r._field == "diag_capture_ok")')
        refresh = records(base + ' |> filter(fn: (r) => r._field == "diag_refresh_applied")')
        valid_ticks = sorted(int(item["_value"]) for item in ticks)
        actual_refresh_ticks = sorted(int(item["acceptance_tick"]) for item in refresh if item["_value"] is True)
        expected_refresh_ticks = [row["tick_seq"] for row in rows if row["diagnostics"]["fields"]["diag_refresh_applied"]]
        checks.append({"name": "native_unique_ticks", "status": "PASS" if valid_ticks == list(range(1, len(rows) + 1)) else "FAIL",
                       "point_count": len(ticks), "unique_ticks": len(set(valid_ticks)),
                       "original_utc_and_tick_tag_match": all(item["_time"] == datetime.fromisoformat(rows[int(item["_value"]) - 1]["computed_at"].replace("Z", "+00:00"))
                          and item.get("acceptance_tick") == str(item["_value"]) for item in ticks)})
        checks[-1]["status"] = "PASS" if checks[-1]["status"] == "PASS" and checks[-1]["original_utc_and_tick_tag_match"] else "FAIL"
        checks.append({"name": "capture_availability", "status": "PASS" if len(capture) == len(rows) and all(item["_value"] is True for item in capture) else "FAIL",
                       "points": len(capture)})
        checks.append({"name": "committed_refresh_ticks", "status": "PASS" if actual_refresh_ticks == expected_refresh_ticks else "FAIL",
                       "ticks": actual_refresh_ticks})
        for panel in panels(dashboard["panels"]):
            for target in panel.get("targets", []):
                query = target["query"].replace("v.defaultBucket", json.dumps(settings.bucket))
                query = query.replace("v.timeRangeStart", start.isoformat().replace("+00:00", "Z"))
                query = query.replace("v.timeRangeStop", stop.isoformat().replace("+00:00", "Z"))
                query = query.replace("${run_id:regex}", re.escape(report["run_id"]))
                try:
                    values = records(query)
                    isolated = all(item.get("run_id") == report["run_id"] for item in values)
                    camera_only = 'r._measurement == "gsensor_measurement"' in query
                    expected = (0 if camera_only else 11 * len(expected_refresh_ticks)
                                if panel["id"] == 23 else 3 * len(rows)
                                if panel["id"] == 24 else len(rows))
                    correct_count = len(values) == expected
                    valid = isolated and correct_count
                    if panel["id"] == 23:
                        valid = valid and all(item["actual"] == item["reference"] and item["snapshot_stage"] == "refresh_init" for item in values)
                    elif panel["id"] == 21:
                        boundary_times = {datetime.fromisoformat(rows[tick - 1]["computed_at"].replace("Z", "+00:00")) for tick in expected_refresh_ticks}
                        boundaries = [item for item in values if item["_time"] in boundary_times]
                        valid = valid and len(boundaries) == len(expected_refresh_ticks) and all(
                            value == 1.0 for item in boundaries for name, value in item.items() if " / initial" in name)
                    checks.append({"name": f'panel_{panel["id"]}_{target["refId"]}', "title": panel["title"],
                                   "status": "PASS" if valid else "FAIL", "rows": len(values), "expected_rows": expected,
                                   "run_isolation": isolated,
                                   "source_status": "NOT RUN: camera source absent in simulated-growth acceptance" if camera_only else "collected"})
                except Exception as exc:
                    checks.append({"name": f'panel_{panel["id"]}_{target["refId"]}', "title": panel["title"],
                                   "status": "FAIL", "error_class": type(exc).__name__})
    return {"status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
            "run_id": report["run_id"], "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Native database integrity and every original/new panel query; browser render verified separately.", "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.run)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
