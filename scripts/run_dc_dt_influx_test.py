from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from crystallization_mpc.infra.influxdb.write import InfluxWriter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write figure-5 dc_dt test data to InfluxDB.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between writes.")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples to write. 0 means run forever.")
    parser.add_argument("--amplitude", type=float, default=1.5e-6, help="Main dc_dt oscillation amplitude.")
    parser.add_argument("--frequency", type=float, default=0.01, help="Main dc_dt frequency in Hz.")
    parser.add_argument("--noise", type=float, default=0.05e-6, help="Uniform noise amplitude for dc_dt.")
    parser.add_argument("--baseline", type=float, default=0.0, help="Baseline dc_dt value.")
    parser.add_argument("--run-id", default="test_dc_dt_001", help="InfluxDB run_id tag.")
    parser.add_argument("--source", default="test_sine", help="InfluxDB source tag.")
    parser.add_argument("--mode", default="test", help="InfluxDB mode tag.")
    parser.add_argument("--target", default="sigma", help="InfluxDB target tag.")
    return parser.parse_args()


def generate_dc_dt_fields(
    elapsed_seconds: float,
    *,
    amplitude: float,
    frequency: float,
    noise: float,
    baseline: float,
) -> dict[str, float]:
    phase = 2 * math.pi * frequency * elapsed_seconds
    dc_dt = baseline + amplitude * math.sin(phase) + random.uniform(-noise, noise)
    dc_dt_KF = baseline + 0.9 * amplitude * math.sin(phase + 0.15)
    return {
        "dc_dt": dc_dt,
        "dc_dt_KF": dc_dt_KF,
    }


def main() -> int:
    args = parse_args()
    sample_index = 0
    start_time = time.monotonic()

    print(
        "Writing figure-5 dc_dt test data to InfluxDB "
        f"(interval={args.interval}s, samples={'infinite' if args.samples == 0 else args.samples})."
    )

    try:
        with InfluxWriter() as writer:
            while args.samples == 0 or sample_index < args.samples:
                elapsed = time.monotonic() - start_time
                fields = generate_dc_dt_fields(
                    elapsed,
                    amplitude=args.amplitude,
                    frequency=args.frequency,
                    noise=args.noise,
                    baseline=args.baseline,
                )
                writer.write_fields(
                    fields,
                    source=args.source,
                    run_id=args.run_id,
                    mode=args.mode,
                    target=args.target,
                )
                sample_index += 1
                print(
                    f"[{sample_index}] "
                    f"dc_dt={fields['dc_dt']:.8e}, "
                    f"dc_dt_KF={fields['dc_dt_KF']:.8e}"
                )
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
