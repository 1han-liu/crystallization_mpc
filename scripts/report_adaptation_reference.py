"""Compare Python with the real R2021a edge-case oracle, without MATLAB at runtime."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.io import loadmat

from crystallization_mpc.apps.controller.algorithm.adaptation import AdaptationError, adapt_growth_parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/controller/fixtures/matlab_r2021a_adaptation_edges.mat"
    oracle = loadmat(fixture, simplify_cells=True)
    rows = []
    for r in oracle["rows"]:
        identifier = ""
        actual = None
        try:
            actual, count = adapt_growth_parameters(r["initial_params"], r["growth"], r["sigma"],
                r["temperature"], r["selected"], int(r["maximum"]), int(r["minimum"]), r["mode"])
            outcome_match = bool(r["ok"]) and count == r["count"]
        except AdaptationError as error:
            identifier = error.reference_identifier
            outcome_match = not r["ok"] and identifier == r["identifier"]
        errors = ({k: abs(actual[k] - float(r[k])) / abs(float(r[k]))
                   for k in ("E_A", "k_0", "n")} if actual is not None and r["ok"] else {})
        rows.append({"case": r["name"], "mode": r["mode"], "outcome_match": bool(outcome_match),
                     "reference_success": bool(r["ok"]), "reference_identifier": str(r["identifier"]),
                     "python_identifier": identifier, "relative_parameter_errors": errors,
                     "numerical_match": all(v <= 1e-5 for v in errors.values()) if errors else None})
    report = {"matlab": oracle["metadata"], "rtol": 1e-5, "atol": 0,
              "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
              "outcome_pass": sum(r["outcome_match"] for r in rows), "outcome_total": len(rows),
              "numerical_pass": sum(r["numerical_match"] is True for r in rows),
              "numerical_fail": sum(r["numerical_match"] is False for r in rows),
              "max_relative_error": max(v for r in rows for v in r["relative_parameter_errors"].values()),
              "status": "PASS" if all(r["outcome_match"] and r["numerical_match"] is not False for r in rows) else "FAIL",
              "scope": "Same-input function oracle, not full MATLAB/Python closed-loop trajectory parity",
              "rows": rows}
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print({k: v for k, v in report.items() if k != "rows"})
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
