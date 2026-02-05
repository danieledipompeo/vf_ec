import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create logs directory if it doesn't exist
Path("logs").mkdir(exist_ok=True)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.DEBUG)

if not _root_logger.handlers:
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)

    file_handler = RotatingFileHandler("logs/app.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    _root_logger.addHandler(console)
    _root_logger.addHandler(file_handler)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
