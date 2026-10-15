"""Process setup shared by every entry point: OS certificate store, .env loading and typed env readers.

Every package imports this first instead of repeating the setup; Python runs it once per process."""

import logging
import os

import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

from dotenv import load_dotenv  # noqa: E402 - after truststore, so nothing opens an SSL context before it

load_dotenv()

log = logging.getLogger("common.env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        log.warning("Invalid %s=%r, using %d", name, os.getenv(name), default)
        return default
