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
    parser = argparse.ArgumentParser(description="Write figure-6 target sigma test data to InfluxDB.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between writes.")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples to write. 0 means run forever.")
    parser.add_argument("--baseline", type=float, default=1.2, help="Baseline target sigma value.")
    parser.add_argument("--amplitude", type=float, default=0.08, help="Target sigma oscillation amplitude.")
    parser.add_argument("--frequency", type=float, default=0.01, help="Target sigma frequency in Hz.")
    parser.add_argument("--noise", type=float, default=0.005, help="Uniform noise amplitude for target_sigma.")
    parser.add_argument("--set-offset", type=float, default=0.015, help="Offset between actual and set sigma.")
    parser.add_argument("--fval-scale", type=float, default=1e-6, help="Scale for f_val_sigma.")
    parser.add_argument("--run-id", default="test_target_sigma_001", help="InfluxDB run_id tag.")
    parser.add_argument("--source", default="test_sine", help="InfluxDB source tag.")
    parser.add_argument("--mode", default="test", help="InfluxDB mode tag.")
    parser.add_argument("--target", default="sigma", help="InfluxDB target tag.")
    return parser.parse_args()


def generate_target_sigma_fields(
    elapsed_seconds: float,
    *,
    baseline: float,
    amplitude: float,
    frequency: float,
    noise: float,
    set_offset: float,
    fval_scale: float,
) -> dict[str, float]:
    phase = 2 * math.pi * frequency * elapsed_seconds
    target_sigma_set = baseline + amplitude * math.sin(phase)
    target_sigma = target_sigma_set - set_offset + 0.01 * math.sin(phase * 1.7)
    target_sigma += random.uniform(-noise, noise)
    f_val_sigma = fval_scale * (1.2 + 0.5 * math.cos(phase * 0.8))
    return {
        "target_sigma": target_sigma,
        "target_sigma_set": target_sigma_set,
        "f_val_sigma": f_val_sigma,
    }


def main() -> int:
    args = parse_args()
    sample_index = 0
    start_time = time.monotonic()

    print(
        "Writing figure-6 target sigma test data to InfluxDB "
        f"(interval={args.interval}s, samples={'infinite' if args.samples == 0 else args.samples})."
    )

    try:
        with InfluxWriter() as writer:
            while args.samples == 0 or sample_index < args.samples:
                elapsed = time.monotonic() - start_time
                fields = generate_target_sigma_fields(
                    elapsed,
                    baseline=args.baseline,
                    amplitude=args.amplitude,
                    frequency=args.frequency,
                    noise=args.noise,
                    set_offset=args.set_offset,
                    fval_scale=args.fval_scale,
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
                    f"target_sigma={fields['target_sigma']:.4f}, "
                    f"target_sigma_set={fields['target_sigma_set']:.4f}, "
                    f"f_val_sigma={fields['f_val_sigma']:.8e}"
                )
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
