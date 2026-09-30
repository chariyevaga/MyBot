from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO", file: str | None = "logs/bot.log") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FMT))
    root.addHandler(console)
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(file, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FMT))
        root.addHandler(fh)
    for noisy in ("urllib3", "ccxt", "psycopg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
