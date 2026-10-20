"""Explicit no-network signal-delivery probe for an isolated managed user unit.

This does not create an experiment or establish Prosys cleanup correctness.
It exercises the managed runner's actual latch under systemd/inhibit and leaves
exclusive, independently readable evidence even when its exit code is 143.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _deny_external_actions(event, args):
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind", "socket.getaddrinfo",
                 "subprocess.Popen", "os.system", "os.kill", "os.killpg"}:
        raise RuntimeError("The synthetic signal probe forbids network/process actions.")


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def run_probe(output, latch, *, hold_seconds=180.0, cleanup_seconds=3.0,
              monotonic=time.monotonic, sleep=time.sleep):
    """No handlers installed here; the CLI installs the production latch."""
    if (not math.isfinite(hold_seconds) or not 0 < hold_seconds <= 300
            or not math.isfinite(cleanup_seconds) or not 0 <= cleanup_seconds <= 10):
        raise ValueError("Synthetic hold must be <=300 seconds; simulated cleanup <=10 seconds.")
    output = Path(output)
    output.mkdir(parents=False, exist_ok=False)
    identity = {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgid(0),
                "sid": os.getsid(0), "cgroup": Path("/proc/self/cgroup").read_text(),
                "started_at": _stamp(), "scope": "No-network signal probe; no Prosys or experiment operations."}
    _save(output / "started.json", identity)
    print(json.dumps({"probe": "READY", **identity}), flush=True)
    deadline = monotonic() + hold_seconds
    while latch.first_signal is None and monotonic() < deadline:
        sleep(min(.05, max(0, deadline - monotonic())))
    interrupted = latch.first_signal is not None
    observed = {"observed_at": _stamp(), "first_signal": latch.first_signal,
                "count": latch.count, "last_signal": latch.last_signal}
    _save(output / "signal_observed.json", observed)
    print(json.dumps({"probe": "SIGNAL_LATCHED" if interrupted else "NO_SIGNAL", **observed}), flush=True)
    cleanup_deadline = monotonic() + cleanup_seconds
    while monotonic() < cleanup_deadline:
        sleep(min(.05, max(0, cleanup_deadline - monotonic())))
    # A first signal during the simulated cleanup is still an interruption.
    # The earlier observation remains an honest point-in-time record.
    interrupted = latch.first_signal is not None
    exit_code = 128 + latch.first_signal if interrupted else 4
    final = {"identity": identity, "finished_at": _stamp(), "completion": "INTERRUPTED" if interrupted else "NO_SIGNAL",
             "first_signal": latch.first_signal, "last_signal": latch.last_signal,
             "count": latch.count, "simulated_cleanup_completed": True,
             "exit_code": exit_code, "prosys_cleanup": "NOT_RUN", "experiment_created": False}
    _save(output / "final.json", final)
    print(json.dumps({"probe": "FINISHED", **final}), flush=True)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=float, default=180)
    parser.add_argument("--cleanup-seconds", type=float, default=3)
    args = parser.parse_args(argv)
    if not args.probe:
        parser.error("Explicit --probe required; no unit or experiment is launched here.")
    sys.addaudithook(_deny_external_actions)
    from integration_tests.prosys.managed_paper_run import SignalLatch
    latch = SignalLatch()
    previous = {signum: signal.signal(signum, latch.handler) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run_probe(args.output, latch, hold_seconds=args.hold_seconds, cleanup_seconds=args.cleanup_seconds)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
