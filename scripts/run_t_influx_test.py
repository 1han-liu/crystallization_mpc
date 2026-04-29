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
    parser = argparse.ArgumentParser(description="Write figure-1 temperature test data to InfluxDB.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between writes.")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples to write. 0 means run forever.")
    parser.add_argument("--baseline", type=float, default=25.0, help="Baseline process temperature in degC.")
    parser.add_argument("--amplitude", type=float, default=2.0, help="Main sine-wave amplitude in degC.")
    parser.add_argument("--frequency", type=float, default=0.01, help="Main sine-wave frequency in Hz.")
    parser.add_argument("--noise", type=float, default=0.05, help="Uniform noise amplitude.")
    parser.add_argument("--run-id", default="test_T_001", help="InfluxDB run_id tag.")
    parser.add_argument("--source", default="test_sine", help="InfluxDB source tag.")
    parser.add_argument("--mode", default="test", help="InfluxDB mode tag.")
    parser.add_argument("--target", default="sigma", help="InfluxDB target tag.")
    return parser.parse_args()


def generate_temperature_fields(
    elapsed_seconds: float,
    *,
    baseline: float,
    amplitude: float,
    frequency: float,
    noise: float,
) -> dict[str, float]:
    phase = 2 * math.pi * frequency * elapsed_seconds
    T = baseline + amplitude * math.sin(phase) + random.uniform(-noise, noise)
    T_KF = baseline + (amplitude * 0.9) * math.sin(phase + 0.08)
    T_j = baseline - 1.2 + (amplitude * 0.6) * math.sin(phase - 0.35)
    T_j_set = baseline - 0.8 + (amplitude * 0.4) * math.sin(phase + 0.45)
    return {
        "T": T,
        "T_KF": T_KF,
        "T_j": T_j,
        "T_j_set": T_j_set,
    }


def main() -> int:
    args = parse_args()
    sample_index = 0
    start_time = time.monotonic()

    print(
        "Writing figure-1 temperature test data to InfluxDB "
        f"(interval={args.interval}s, samples={'infinite' if args.samples == 0 else args.samples})."
    )

    try:
        with InfluxWriter() as writer:
            while args.samples == 0 or sample_index < args.samples:
                elapsed = time.monotonic() - start_time
                fields = generate_temperature_fields(
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
                print(
                    f"[{sample_index}] "
                    f"T={fields['T']:.2f}, "
                    f"T_KF={fields['T_KF']:.2f}, "
                    f"T_j={fields['T_j']:.2f}, "
                    f"T_j_set={fields['T_j_set']:.2f}"
                )
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
