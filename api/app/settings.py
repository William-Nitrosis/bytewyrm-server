import os
from pathlib import Path


DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", "/data/database/server.db")
)

# Absolute safety ceiling for any request body reaching the application.
# Individual ByteWyrm projects may choose a smaller limit.
HARD_MAX_REQUEST_SIZE = 2 * 1024

# Project defaults. These are intentionally conservative and can be
# overridden when a container is created with manage.py.
DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024
DEFAULT_READ_RATE_LIMIT = 100
DEFAULT_WRITE_RATE_LIMIT = 20

# Generic schema safety limits.
MAX_FIELDS_PER_CONTAINER = 16
MAX_FIELD_NAME_LENGTH = 32
MAX_TEXT_LENGTH = 512
DEFAULT_TEXT_MAX_LENGTH = 128

# SQLite INTEGER is a signed 64-bit value.
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1
