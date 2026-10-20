"""Explicit host-only paper runner with named, revalidated integration evidence.

Exit codes: 0 PASS, 2 FAIL, 3 REVIEW_REQUIRED, 4 INCOMPLETE, 5 rejected CLI.
Only --run-paper authorizes starting a new experiment. No production code is
patched and the ordinary same-source/same-seed short-dynamic gate is unchanged.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crystallization_mpc.testing.prosys.runner import ProsysRunner, RunnerConfig
from integration_tests.prosys.integration_qualification import (
    ROOT, QualificationError, digest, evaluate_bundle, strict_json, tool_hashes,
)


EXIT_CODES = {"PASS": 0, "FAIL": 2, "REVIEW_REQUIRED": 3, "INCOMPLETE": 4}


class ExplicitParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(5, f"{self.prog}: rejected before experiment creation: {message}\n")


class QualifiedPaperRunner(ProsysRunner):
    def __init__(self, config, *, integration_qualification: Path, qualification_verifier=evaluate_bundle,
                 root=ROOT, **kwargs):
        if config.mode != "paper":
            raise QualificationError("Qualified wrapper supports paper mode only.")
        self.qualification_path = integration_qualification
        self.qualification_verifier = qualification_verifier
        self.qualification_root = root
        self.qualification = None
        self.qualification_sha256 = None
        self.wrapper_hashes = tool_hashes(root)
        super().__init__(config, **kwargs)

    def preflight(self):
        content = self.qualification_path.read_bytes()
        bundle = strict_json(content)
        result = self.qualification_verifier(bundle, self.qualification_root, current=self.initial_sources, live=True)
        if (result.get("safety_status") != "PASS" or result.get("provenance_status") != "PASS"
                or result.get("runtime", {}).get("status") != "PASS" or result.get("eligible_for_paper") is not True):
            raise QualificationError("Required named integration evidence/provenance is incomplete or failed; no experiment mutation allowed.")
        self.qualification_sha256 = digest(content)
        self.qualification = result
        self.log.append("integration_qualification", {"path": str(self.qualification_path),
            "sha256": self.qualification_sha256, "verification": result, "host_tools": self.wrapper_hashes})
        super().preflight()
        # This is the narrow safety/recovery check, not a fabricated complete
        # archive or control-performance PASS. The structured result stays raw.
        self.gates.record_check("integration", "failure_recovery", "PASS",
            f"Named captured safety cases re-evaluated; evidence {self.qualification_path}; sha256 {self.qualification_sha256}; "
            f"artifact_status={result['artifact_status']}, overall={result['overall']}. See integration_qualification evidence and qualified_report.json for retained acquisition-outage limitations.")

    def prepare(self):
        # Close the file-mutation gap between the read-only preflight and the
        # first experiment creation. Never silently re-use an altered bundle.
        if self.qualification is None or not self._immutability_intact():
            raise QualificationError("Qualification or wrapper changed before the first mutation.")
        return super().prepare()

    def _immutability_intact(self):
        try:
            return (tool_hashes(self.qualification_root) == self.wrapper_hashes
                and (self.qualification_sha256 is None or digest(self.qualification_path.read_bytes()) == self.qualification_sha256))
        except (OSError, ValueError, QualificationError):
            return False

    def write_report(self):
        if not self._immutability_intact():
            self.gates.fail("Host wrapper/qualification changed during the paper experiment.")
        ordinary = super().write_report()
        qualified = {**ordinary, "ordinary_report_sha256": digest(self.report_path.read_bytes()),
            "integration_qualification": {"path": str(self.qualification_path), "sha256": self.qualification_sha256,
                "verification": self.qualification, "host_tools": self.wrapper_hashes},
            "exit_code": EXIT_CODES.get(ordinary["overall"], 4)}
        # Original report remains byte-identical. This extra report makes the
        # independently scoped safety/artifact distinction explicit.
        with (self.config.output / "qualified_report.json").open("x", encoding="utf-8") as stream:
            json.dump(qualified, stream, indent=2, allow_nan=False)
            stream.write("\n")
        return qualified


def main(argv=None):
    parser = ExplicitParser(description=__doc__)
    parser.add_argument("--run-paper", action="store_true")
    parser.add_argument("--integration-qualification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, choices=(123, 456), required=True)
    parser.add_argument("--max-hours", type=float, default=6)
    parser.add_argument("--commissioning-evidence", type=Path, required=True)
    parser.add_argument("--dynamic-evidence", type=Path, required=True)
    parser.add_argument("--telemetry-config", type=Path, default=Path(".runtime/prosys-test/telemetry.json"))
    args = vars(parser.parse_args(argv))
    if args.pop("run_paper") is not True:
        parser.error("Explicit --run-paper is required; no service action occurred.")
    qualification = args.pop("integration_qualification")
    config = RunnerConfig(mode="paper", **args)
    runner = QualifiedPaperRunner(config, integration_qualification=qualification)
    report = runner.run()
    print(json.dumps({"overall": report["overall"], "exit_code": report["exit_code"],
        "qualified_report": str(config.output / "qualified_report.json"), "grafana_url": report.get("grafana_url")}))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
