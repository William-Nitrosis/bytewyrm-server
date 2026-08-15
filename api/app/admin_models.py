import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from settings import (
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_READ_RATE_LIMIT,
    DEFAULT_TEXT_MAX_LENGTH,
    DEFAULT_WRITE_RATE_LIMIT,
    HARD_MAX_REQUEST_SIZE,
    MAX_TEXT_LENGTH,
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
)


FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class StrictAdminModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProjectCreate(StrictAdminModel):
    name: str = Field(min_length=1, max_length=80)
    store_max_records: int = Field(default=DEFAULT_MAX_RECORDS, ge=1, le=100_000)
    store_overflow_policy: Literal["reject", "delete_oldest"] = "reject"
    max_request_bytes: int = Field(
        default=DEFAULT_MAX_REQUEST_BYTES,
        ge=128,
        le=HARD_MAX_REQUEST_SIZE,
    )
    read_rate_limit: int = Field(default=DEFAULT_READ_RATE_LIMIT, ge=1, le=10_000)
    write_rate_limit: int = Field(default=DEFAULT_WRITE_RATE_LIMIT, ge=1, le=10_000)


class ProjectUpdate(StrictAdminModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    store_max_records: int | None = Field(default=None, ge=1, le=100_000)
    store_overflow_policy: Literal["reject", "delete_oldest"] | None = None
    store_record_mode: Literal[
        "append", "replace_latest", "keep_highest", "keep_lowest"
    ] | None = None
    store_key_field: str | None = Field(default=None, min_length=1, max_length=32)
    store_compare_field: str | None = Field(default=None, min_length=1, max_length=32)
    store_read_scope: Literal["project", "own_key"] | None = None
    store_owner_only: bool | None = None
    max_request_bytes: int | None = Field(
        default=None,
        ge=128,
        le=HARD_MAX_REQUEST_SIZE,
    )
    read_rate_limit: int | None = Field(default=None, ge=1, le=10_000)
    write_rate_limit: int | None = Field(default=None, ge=1, le=10_000)


class FieldCreate(StrictAdminModel):
    name: str = Field(min_length=1, max_length=32)
    type: Literal["integer", "float", "boolean", "text"]
    required: bool = True

    integer_min: int | None = Field(default=None, ge=SQLITE_INT_MIN, le=SQLITE_INT_MAX)
    integer_max: int | None = Field(default=None, ge=SQLITE_INT_MIN, le=SQLITE_INT_MAX)
    float_min: float | None = None
    float_max: float | None = None
    text_min_length: int | None = Field(default=None, ge=0, le=MAX_TEXT_LENGTH)
    text_max_length: int | None = Field(default=None, ge=1, le=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def validate_constraints(self):
        if FIELD_NAME_RE.fullmatch(self.name) is None:
            raise ValueError(
                "field names must start with a lowercase letter and contain only "
                "lowercase letters, numbers, and underscores"
            )

        if self.type == "integer":
            if any(v is not None for v in (self.float_min, self.float_max, self.text_min_length, self.text_max_length)):
                raise ValueError("integer fields may only use integer_min/integer_max")
            if self.integer_min is not None and self.integer_max is not None and self.integer_min > self.integer_max:
                raise ValueError("integer_min cannot be greater than integer_max")

        elif self.type == "float":
            if any(v is not None for v in (self.integer_min, self.integer_max, self.text_min_length, self.text_max_length)):
                raise ValueError("float fields may only use float_min/float_max")
            if self.float_min is not None and not math.isfinite(self.float_min):
                raise ValueError("float_min must be finite")
            if self.float_max is not None and not math.isfinite(self.float_max):
                raise ValueError("float_max must be finite")
            if self.float_min is not None and self.float_max is not None and self.float_min > self.float_max:
                raise ValueError("float_min cannot be greater than float_max")

        elif self.type == "boolean":
            if any(
                v is not None
                for v in (
                    self.integer_min,
                    self.integer_max,
                    self.float_min,
                    self.float_max,
                    self.text_min_length,
                    self.text_max_length,
                )
            ):
                raise ValueError("boolean fields do not accept value constraints")

        elif self.type == "text":
            if any(v is not None for v in (self.integer_min, self.integer_max, self.float_min, self.float_max)):
                raise ValueError("text fields may only use text length constraints")
            minimum = 0 if self.text_min_length is None else self.text_min_length
            maximum = DEFAULT_TEXT_MAX_LENGTH if self.text_max_length is None else self.text_max_length
            if minimum > maximum:
                raise ValueError("text_min_length cannot be greater than text_max_length")
            self.text_min_length = minimum
            self.text_max_length = maximum

        return self


class KeyCreate(StrictAdminModel):
    name: str = Field(min_length=1, max_length=80)
    client_name: str | None = Field(default=None, min_length=1, max_length=80)
    permissions: Literal["r", "w", "rw", "wr"] = "rw"
