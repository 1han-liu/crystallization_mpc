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
    parser = argparse.ArgumentParser(description="Write figure-4 concentration test data to InfluxDB.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between writes.")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples to write. 0 means run forever.")
    parser.add_argument("--baseline", type=float, default=0.18, help="Baseline concentration value in g/gH2O.")
    parser.add_argument("--amplitude", type=float, default=0.01, help="Concentration oscillation amplitude.")
    parser.add_argument("--frequency", type=float, default=0.01, help="Concentration frequency in Hz.")
    parser.add_argument("--noise", type=float, default=0.0004, help="Uniform noise amplitude for c.")
    parser.add_argument("--run-id", default="test_c_001", help="InfluxDB run_id tag.")
    parser.add_argument("--source", default="test_sine", help="InfluxDB source tag.")
    parser.add_argument("--mode", default="test", help="InfluxDB mode tag.")
    parser.add_argument("--target", default="sigma", help="InfluxDB target tag.")
    return parser.parse_args()


def generate_c_fields(
    elapsed_seconds: float,
    *,
    baseline: float,
    amplitude: float,
    frequency: float,
    noise: float,
) -> dict[str, float]:
    phase = 2 * math.pi * frequency * elapsed_seconds
    c = baseline + amplitude * math.sin(phase) + random.uniform(-noise, noise)
    c_KF = baseline + 0.92 * amplitude * math.sin(phase + 0.1)
    return {
        "c": c,
        "c_KF": c_KF,
    }


def main() -> int:
    args = parse_args()
    sample_index = 0
    start_time = time.monotonic()

    print(
        "Writing figure-4 concentration test data to InfluxDB "
        f"(interval={args.interval}s, samples={'infinite' if args.samples == 0 else args.samples})."
    )

    try:
        with InfluxWriter() as writer:
            while args.samples == 0 or sample_index < args.samples:
                elapsed = time.monotonic() - start_time
                fields = generate_c_fields(
                    elapsed,
                    baseline=args.baseline,
                    amplitude=args.amplitude,
                    frequency=args.frequency,
                    noise=args.noise,
                )
                writer.write_fields(
                    fields,
                    source=args.source,
                    run_id=args.run_id,
                    mode=args.mode,
                    target=args.target,
                )
                sample_index += 1
                print(f"[{sample_index}] c={fields['c']:.5f}, c_KF={fields['c_KF']:.5f}")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
