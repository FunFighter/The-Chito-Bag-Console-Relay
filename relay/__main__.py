import logging
import sys

from .bot import Relay
from .config import ConfigError, load


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        cfg = load()
    except ConfigError as exc:
        logging.error("configuration: %s", exc)
        return 2
    Relay(cfg).run(cfg.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
