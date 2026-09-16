"""Read only the explicitly allowed local Controller telemetry environment."""
from pathlib import Path
import shlex

KEYS = {'CONTROLLER_INFLUX_ENABLED', 'CONTROLLER_INFLUX_URL',
        'CONTROLLER_INFLUX_ORG', 'CONTROLLER_INFLUX_BUCKET', 'CONTROLLER_INFLUX_TOKEN'}


def load_telemetry_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.strip().removeprefix('export ').partition('=')
        if separator and key in KEYS:
            words = shlex.split(value, comments=True)
            if len(words) != 1:
                raise ValueError('Invalid local telemetry configuration.')
            values[key] = words[0]
    if values.get('CONTROLLER_INFLUX_ENABLED') != 'true' or set(values) != KEYS:
        raise ValueError('Local telemetry configuration is incomplete.')
    return values
