"""Run with python -m crystallization_mpc.apps.opcua_writer."""

import logging
import signal

from .config import WriterSettings
from .service import WriterService


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = WriterService(WriterSettings.from_env())
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: service.stop())
    service.run()


if __name__ == "__main__":
    main()
