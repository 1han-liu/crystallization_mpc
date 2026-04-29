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
    parser = argparse.ArgumentParser(description="Write figure-7 target G test data to InfluxDB.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between writes.")
    parser.add_argument("--samples", type=int, default=0, help="Number of samples to write. 0 means run forever.")
    parser.add_argument("--baseline", type=float, default=2.5e-7, help="Baseline target G value.")
    parser.add_argument("--amplitude", type=float, default=0.6e-7, help="Target G oscillation amplitude.")
    parser.add_argument("--frequency", type=float, default=0.01, help="Target G frequency in Hz.")
    parser.add_argument("--noise", type=float, default=0.03e-7, help="Uniform noise amplitude for target_G.")
    parser.add_argument("--set-offset", type=float, default=0.08e-7, help="Offset between actual and set G.")
    parser.add_argument("--fval-scale", type=float, default=1e-8, help="Scale for f_val_G.")
    parser.add_argument("--run-id", default="test_target_G_001", help="InfluxDB run_id tag.")
    parser.add_argument("--source", default="test_sine", help="InfluxDB source tag.")
    parser.add_argument("--mode", default="test", help="InfluxDB mode tag.")
    parser.add_argument("--target", default="G", help="InfluxDB target tag.")
    return parser.parse_args()


def generate_target_g_fields(
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
    target_G_set = baseline + amplitude * math.sin(phase)
    target_G = target_G_set - set_offset + 0.04e-7 * math.sin(phase * 1.6)
    target_G += random.uniform(-noise, noise)
    f_val_G = fval_scale * (1.1 + 0.45 * math.cos(phase * 0.9))
    return {
        "target_G": target_G,
        "target_G_set": target_G_set,
        "f_val_G": f_val_G,
    }


def main() -> int:
    args = parse_args()
    sample_index = 0
    start_time = time.monotonic()

    print(
        "Writing figure-7 target G test data to InfluxDB "
        f"(interval={args.interval}s, samples={'infinite' if args.samples == 0 else args.samples})."
    )

    try:
        with InfluxWriter() as writer:
            while args.samples == 0 or sample_index < args.samples:
                elapsed = time.monotonic() - start_time
                fields = generate_target_g_fields(
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
                    f"target_G={fields['target_G']:.8e}, "
                    f"target_G_set={fields['target_G_set']:.8e}, "
                    f"f_val_G={fields['f_val_G']:.8e}"
                )
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
