#!/usr/bin/env python3
"""Read-only audit of an instrumented run against its pre-diagnostic baseline.

Outputs an independent evidence report to stdout. Never adjusts acceptance
criteria, rewrites run data, or treats chart validity as control success.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def read_rows(directory: Path) -> list[dict]:
    with (directory / "ticks.jsonl").open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def audit(baseline: Path, run: Path, dashboard: Path) -> dict:
    old, new = read_rows(baseline), read_rows(run)
    checks: dict[str, dict] = {}

    def check(name: str, condition: bool, **evidence) -> None:
        checks[name] = {"status": "PASS" if condition else "FAIL", **evidence}

    check("tick_identity", len(old) == len(new) > 0
          and [row["tick_seq"] for row in new] == list(range(1, len(new) + 1)),
          baseline_ticks=len(old), instrumented_ticks=len(new))
    old_params = json.loads((baseline / "parameters.json").read_text())
    new_params = json.loads((run / "parameters.json").read_text())
    check("unchanged_parameters", old_params == new_params)
    ignored = {"run_id", "computed_at"}
    differences = []
    compared = 0
    for previous, current in zip(old, new):
        for key, value in previous.items():
            if key in ignored:
                continue
            compared += 1
            if key not in current or current[key] != value:
                differences.append({"tick_seq": previous["tick_seq"], "field": key,
                                    "baseline": value, "instrumented": current.get(key)})
    check("original_tick_values_exactly_unchanged", not differences and len(old) == len(new),
          compared_values=compared, difference_count=len(differences),
          first_differences=differences[:12],
          scope="All original persisted result/gate values; full filter P/call counts verified separately by unit tests.")

    failed = []
    refresh_ticks = []
    sizes = {"main": 4, "G": 3, "E_A": 3, "k_0": 3, "n": 3}
    previous_count = 0
    for row in new:
        diagnostic = row.get("diagnostics") or {}
        fields, raw = diagnostic.get("fields", {}), diagnostic.get("raw", {})
        if not row.get("diag_capture_ok") or diagnostic.get("tick_seq") != row["tick_seq"]:
            failed.append([row["tick_seq"], "missing/mismatched snapshot"])
            continue
        if diagnostic.get("run_id") != row["run_id"] or diagnostic.get("elapsed_s") != row["elapsed_s"]:
            failed.append([row["tick_seq"], "run/elapsed identity mismatch"])
        refreshed = row["refresh_count"] > previous_count
        previous_count = row["refresh_count"]
        if fields.get("diag_refresh_applied") is not refreshed:
            failed.append([row["tick_seq"], "refresh metadata mismatch"])
        if not refreshed:
            if raw.get("refresh_init") is not None:
                failed.append([row["tick_seq"], "old initialization carried to posterior"])
            continue
        refresh_ticks.append(row["tick_seq"])
        initialized = raw.get("refresh_init") or {}
        if set(initialized) != set(sizes):
            failed.append([row["tick_seq"], "missing five initialization snapshots"])
        if fields.get("diag_snapshot_stage") != "refresh_init":
            failed.append([row["tick_seq"], "stage is not initialization"])
        expected = [row["T"], 0.0, row["c"], 0.0]
        if initialized.get("main", {}).get("state") != expected:
            failed.append([row["tick_seq"], "main measured T/c or derivatives mismatch"])
        for name, size in sizes.items():
            initial = initialized.get(name, {})
            eye = [[float(i == j) for j in range(size)] for i in range(size)]
            if initial.get("covariance") != eye:
                failed.append([row["tick_seq"], name + " complete matrix mismatch"])
            if initial.get("covariance") != raw.get("filters", {}).get(name, {}).get("initial_covariance"):
                failed.append([row["tick_seq"], name + " constructor P mismatch"])
            if name != "main" and initial.get("state") != [0.0] * size:
                failed.append([row["tick_seq"], name + " constructor state mismatch"])
        if any("innovation" in key or key.endswith("_prior") for key in fields):
            failed.append([row["tick_seq"], "fabricated initialization prediction/innovation"])
        if "diag_G_state" in fields or "diag_dT_dt_request" in fields:
            failed.append([row["tick_seq"], "fabricated G correction or control request"])
        if any(fields.get(key) != 0.0 for key in
               ("diag_main_dT_dt_state", "diag_main_dc_dt_state", "diag_int_target", "diag_int_dT_dt", "diag_int_T")):
            failed.append([row["tick_seq"], "derivative/integral reset mismatch"])
        if fields.get("diag_sample_count") != 0:
            failed.append([row["tick_seq"], "new segment contains old samples"])
        if row["tick_seq"] < len(new):
            following = new[row["tick_seq"]]["diagnostics"]
            if following["fields"].get("diag_main_lifecycle") != "updated":
                failed.append([row["tick_seq"], "main did not resume"])
            if "prior" not in following["raw"]["filters"]["main"] or "posterior" not in following["raw"]["filters"]["main"]:
                failed.append([row["tick_seq"], "next predict/correct missing"])
    check("raw_diagnostic_and_initialization_evidence", not failed and bool(refresh_ticks),
          refresh_ticks=refresh_ticks, failures=failed)

    # Inspect real branch observations, not reconstructed controller physics.
    control_failures = []
    branch_counts = {"requested": 0, "zero": 0, "held": 0}
    for row in new:
        diagnostic = row.get("diagnostics") or {}
        fields = diagnostic.get("fields", {})
        control = diagnostic.get("raw", {}).get("control", {})
        branch = control.get("selected_branch")
        if branch not in branch_counts:
            control_failures.append([row["tick_seq"], "missing/unknown selected branch"])
            continue
        branch_counts[branch] += 1
        if fields.get("diag_T_j_selected_branch") != branch or control.get("selected") != row["T_j_set"]:
            control_failures.append([row["tick_seq"], "selected branch/value differs from output"])
        if branch == "held":
            if any(key in fields for key in ("diag_T_j_candidate_raw", "diag_T_j_zero_raw",
                                             "diag_request_physical_limit", "diag_zero_physical_limit")):
                control_failures.append([row["tick_seq"], "fabricated held candidate/limit"])
            continue
        request_clip = control["candidate_raw"] != control["candidate_bounded"]
        if control.get("request_physical_limit") is not request_clip or fields.get("diag_request_physical_limit") is not request_clip:
            control_failures.append([row["tick_seq"], "request clip flag mismatch"])
        zero_evaluated = control.get("zero_raw") is not None
        zero_clip = zero_evaluated and control["zero_raw"] != control["zero_bounded"]
        if zero_evaluated:
            if fields.get("diag_T_j_zero_raw") != control["zero_raw"] or fields.get("diag_T_j_zero_bounded") != control["zero_bounded"]:
                control_failures.append([row["tick_seq"], "zero candidate scalar mismatch"])
            if control.get("zero_physical_limit") is not zero_clip or fields.get("diag_zero_physical_limit") is not zero_clip:
                control_failures.append([row["tick_seq"], "zero clip flag mismatch"])
        elif "diag_zero_physical_limit" in fields or "diag_T_j_zero_raw" in fields:
            control_failures.append([row["tick_seq"], "fabricated unevaluated zero branch"])
        if control.get("physical_limit") is not (request_clip or zero_clip) or fields.get("diag_physical_limit") is not (request_clip or zero_clip):
            control_failures.append([row["tick_seq"], "aggregate clip flag mismatch"])
        expected_branch = "zero" if zero_evaluated and control["candidate_bounded"] > control["zero_bounded"] else "requested"
        selected_value = control["zero_bounded"] if expected_branch == "zero" else control["candidate_bounded"]
        if branch != expected_branch or control["selected"] != selected_value:
            control_failures.append([row["tick_seq"], "branch selection differs from observed candidates"])
    check("control_limit_branch_evidence", not control_failures,
          branch_counts=branch_counts, failures=control_failures,
          aggregate_semantics="OR of actually evaluated candidates; not necessarily selected branch clipping.")

    initial_dashboard = json.loads((baseline / "initial-dashboard.json").read_text())
    final_dashboard = json.loads(dashboard.read_text())
    existing = {panel["id"]: panel for panel in final_dashboard["panels"] if panel["id"] < 15}
    changed_panels = []
    for panel in initial_dashboard["panels"]:
        actual = existing.get(panel["id"])
        digest = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
        if digest != panel["hash"]:
            changed_panels.append(panel["id"])
    check("original_dashboard_preserved", not changed_panels
          and len(existing) == len(initial_dashboard["panels"])
          and final_dashboard["uid"] == initial_dashboard["uid"]
          and final_dashboard["templating"] == initial_dashboard["templating"],
          changed_panel_ids=changed_panels)
    rows = [panel for panel in final_dashboard["panels"] if panel["id"] == 15]
    check("eleven_panels_in_collapsed_row", len(rows) == 1 and rows[0].get("collapsed") is True
          and len(rows[0].get("panels", [])) == 11)
    children = rows[0].get("panels", []) if rows else []
    expected_ids = list(range(16, 27))
    check("diagnostic_layout_and_utc", [panel["id"] for panel in children] == expected_ids
          and all(panel["gridPos"]["w"] == (24 if panel["id"] == 26 else 12)
                  and panel["gridPos"]["x"] == (12 if panel["id"] < 26 and panel["id"] % 2 else 0)
                  for panel in children)
          and final_dashboard.get("timezone") == "utc",
          panel_ids=[panel["id"] for panel in children],
          timezone=final_dashboard.get("timezone"))
    report = json.loads((run / "report.json").read_text())
    return {"status": "PASS" if all(item["status"] == "PASS" for item in checks.values()) else "FAIL",
            "baseline_run": old[0]["run_id"] if old else None,
            "instrumented_run": report["run_id"], "checks": checks,
            "control_acceptance_unchanged": {key: report.get(key) for key in
                ("status", "endpoint_reason", "failure", "control_performance")},
            "note": "Read-only audit; rendered dashboard/DB integrity and regression coverage are separate evidence."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--dashboard", type=Path,
                        default=Path(__file__).resolve().parents[1] / "grafana/dashboards/crystallization-mpc.json")
    args = parser.parse_args()
    result = audit(args.baseline, args.run, args.dashboard)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
