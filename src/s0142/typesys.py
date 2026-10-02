"""Lexical checks for S0142 data-item type strings taken from the workbook.

The workbook stores type names such as ``decimal(10,3)``. It does not define
whether the first number is a count of digits before the decimal point or a
total number of significant digits. Fraction digits (the second number) are
checked. The first number is recorded and not used as a failing limit for
``decimal`` or ``number`` values that also carry a scale.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_NBSP = "\u00a0"
_TYPED = re.compile(
    r"^(?P<kind>decimal|number|integer|text|varchar2)"
    r"\((?P<length>\d+)(?:,(?P<scale>\d+))?\)$"
)
_SIMPLE = frozenset({"date", "datetime", "boolean", "char"})

# The workbook type name is ``boolean``. Cell comments use the word True.
# The accepted spellings below are an assumption; see the README.
_BOOLEAN = frozenset({"T", "F", "TRUE", "FALSE", "True", "False", "true", "false"})
_SIGNED_INT = re.compile(r"^[+-]?\d+$")
_SIGNED_DECIMAL = re.compile(r"^[+-]?(\d+)(?:\.(\d+))?$")


@dataclass(frozen=True)
class ParsedType:
    """A workbook type string, split into the parts the validator can defend."""

    raw: str
    data_type: str
    kind: str
    length_parameter: int | None
    scale: int | None
    length_parameter_enforced: bool

    def to_json(self) -> dict:
        return {
            "raw_type": self.raw,
            "data_type": self.data_type,
            "kind": self.kind,
            "length_parameter": self.length_parameter,
            "scale": self.scale,
            "length_parameter_enforced": self.length_parameter_enforced,
        }


def clean_type_name(raw: str) -> str:
    return str(raw).replace(_NBSP, " ").strip()


def parse_type(raw: str) -> ParsedType:
    original = str(raw)
    data_type = clean_type_name(original)
    if data_type in _SIMPLE:
        return ParsedType(
            raw=original,
            data_type=data_type,
            kind=data_type,
            length_parameter=1 if data_type == "char" else None,
            scale=None,
            length_parameter_enforced=data_type == "char",
        )
    match = _TYPED.fullmatch(data_type)
    if not match:
        raise ValueError(f"unrecognised workbook data type {original!r}")
    kind = match.group("kind")
    length = int(match.group("length"))
    scale_text = match.group("scale")
    scale = int(scale_text) if scale_text is not None else None
    if kind == "integer":
        return ParsedType(original, data_type, kind, length, 0, True)
    if kind in {"text", "varchar2"}:
        return ParsedType(original, data_type, kind, length, None, True)
    if kind == "number" and scale is None:
        return ParsedType(original, data_type, kind, length, 0, True)
    # decimal(n,m) and number(n,m): scale is enforced, n is not.
    if scale is None:
        raise ValueError(f"decimal type missing scale: {original!r}")
    return ParsedType(original, data_type, kind, length, scale, False)


def check_lexical(value: str, parsed: ParsedType) -> str | None:
    """Return ``bad_type``, ``bad_precision``, or None.

    Empty values are the caller's concern. This function does not treat an
    empty string as zero and does not rewrite a sign.
    """

    if value == "":
        return None
    kind = parsed.kind
    if kind == "date":
        return None if _valid_date(value) else "bad_type"
    if kind == "datetime":
        return None if _valid_datetime(value) else "bad_type"
    if kind == "boolean":
        return None if value in _BOOLEAN else "bad_type"
    if kind == "char":
        return None if len(value) == 1 else "bad_type"
    if kind in {"text", "varchar2"}:
        limit = parsed.length_parameter
        if limit is not None and len(value) > limit:
            return "bad_type"
        return None
    if kind == "integer" or (kind == "number" and parsed.scale == 0 and parsed.length_parameter_enforced):
        return _check_integer(value, parsed.length_parameter)
    if kind in {"decimal", "number"}:
        return _check_decimal(value, parsed.scale)
    return "bad_type"


def _check_integer(value: str, length: int | None) -> str | None:
    if not _SIGNED_INT.fullmatch(value):
        return "bad_type"
    digits = value[1:] if value[0] in "+-" else value
    if length is not None and len(digits) > length:
        return "bad_type"
    return None


def _check_decimal(value: str, scale: int | None) -> str | None:
    match = _SIGNED_DECIMAL.fullmatch(value)
    if not match:
        return "bad_type"
    fraction = match.group(2)
    if scale is None:
        return "bad_type"
    if scale == 0:
        if fraction is not None:
            return "bad_precision"
        return None
    if fraction is not None and len(fraction) > scale:
        return "bad_precision"
    return None


def _valid_date(value: str) -> bool:
    if not re.fullmatch(r"\d{8}", value):
        return False
    try:
        dt.datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def _valid_datetime(value: str) -> bool:
    if not re.fullmatch(r"\d{14}", value):
        return False
    try:
        dt.datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return True
