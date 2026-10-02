"""Lexical checks for workbook type strings, using IDD Part 1 v64.0 §2.2.3.

``decimal(n,d)`` allows at most ``d`` digits after the point and at most
``n - d`` digits before it. That second limit is a hard failure.

Leading and trailing zeros are warnings, except the zero spellings Part 1
lists (``0``, ``0.0``, ``.0``, ``0.`` and the same four with a leading
minus). ``T`` and ``F`` are the boolean values; other true/false spellings
are warnings. Text spacing and characters outside the Part 1 set are warnings.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from s0142.part1 import TEXT_CHARS, VALID_DECIMAL_ZEROS

_NBSP = "\u00a0"
_TYPED = re.compile(
    r"^(?P<kind>decimal|number|integer|text|varchar2)"
    r"\((?P<length>\d+)(?:,(?P<scale>\d+))?\)$"
)
_SIMPLE = frozenset({"date", "datetime", "time", "timestamp", "boolean", "char", "null"})
_INTEGER = re.compile(r"^-?\d+$")
_DECIMAL = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)$")
_BOOLEAN_WORDS = frozenset({"T", "F", "TRUE", "FALSE"})


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


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    detail: str


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
    if scale is None:
        raise ValueError(f"decimal type missing scale: {original!r}")
    # The workbook Items sheet does not fill Length for these types, so the
    # flag stays false. check_lexical still applies the Part 1 digit rules.
    return ParsedType(original, data_type, kind, length, scale, False)


def check_lexical(value: str, parsed: ParsedType) -> list[Finding]:
    """Hard failures and style warnings for one non-empty value.

    An empty string is the caller's concern. Nothing here rewrites a sign
    or turns an empty field into zero.
    """

    if value == "":
        return []
    kind = parsed.kind
    if kind == "date":
        return [] if _valid_date(value) else [_error("bad_type", f"date must be YYYYMMDD, got {value!r}")]
    if kind == "datetime":
        return [] if _valid_datetime(value) else [_error("bad_type", f"datetime must be YYYYMMDDHHMMSS, got {value!r}")]
    if kind == "time":
        return [] if _valid_clock(value, "%H%M") else [_error("bad_type", f"time must be HHMM, got {value!r}")]
    if kind == "timestamp":
        return [] if _valid_clock(value, "%H%M%S") else [_error("bad_type", f"timestamp must be HHMMSS, got {value!r}")]
    if kind == "boolean":
        return _check_boolean(value)
    if kind == "char":
        return _check_char(value)
    if kind == "null":
        return [_error("bad_type", "a null-typed field has to be empty")]
    if kind in {"text", "varchar2"}:
        return _check_text(value, parsed.length_parameter)
    if kind == "integer" or (kind == "number" and parsed.scale == 0 and parsed.length_parameter_enforced):
        return _check_integer(value, parsed.length_parameter)
    if kind in {"decimal", "number"}:
        return _check_decimal(value, parsed.length_parameter, parsed.scale)
    return [_error("bad_type", f"unrecognised kind {kind}")]


def _error(code: str, detail: str) -> Finding:
    return Finding("error", code, detail)


def _warn(code: str, detail: str) -> Finding:
    return Finding("warning", code, detail)


def _check_boolean(value: str) -> list[Finding]:
    if value in {"T", "F"}:
        return []
    if value.upper() in _BOOLEAN_WORDS:
        return [_warn("boolean_spelling", f"boolean {value!r} is not T or F")]
    return [_error("bad_type", f"boolean {value!r} is not T or F")]


def _check_char(value: str) -> list[Finding]:
    if len(value) != 1:
        return [_error("bad_type", f"char must be one character, got {value!r}")]
    return _text_style(value)


def _check_text(value: str, limit: int | None) -> list[Finding]:
    findings: list[Finding] = []
    if limit is not None and len(value) > limit:
        findings.append(_error("bad_type", f"text longer than {limit}"))
    findings.extend(_text_style(value))
    return findings


def _text_style(value: str) -> list[Finding]:
    findings: list[Finding] = []
    if value[:1] == " " or value[-1:] == " ":
        findings.append(_warn("text_space", "leading or trailing space"))
    if any(ord(char) not in TEXT_CHARS for char in value):
        findings.append(_warn("text_charset", "character outside the Part 1 text set"))
    return findings


def _check_integer(value: str, length: int | None) -> list[Finding]:
    if not _INTEGER.fullmatch(value):
        return [_error("bad_type", f"integer {value!r} is not an optional minus and digits")]
    digits = value[1:] if value[0] == "-" else value
    findings: list[Finding] = []
    if length is not None and len(digits) > length:
        findings.append(_error("bad_type", f"integer has more than {length} digits"))
    if len(digits) > 1 and digits[0] == "0":
        findings.append(_warn("leading_zero", "leading zero"))
    return findings


def _check_decimal(value: str, length: int | None, scale: int | None) -> list[Finding]:
    if scale is None or not _DECIMAL.fullmatch(value):
        return [_error("bad_type", f"decimal {value!r} does not match the Part 1 picture")]
    sign, ip, fp = _split_decimal(value)
    del sign
    fraction_len = len(fp) if fp is not None else 0
    before = len(ip)
    total = before + fraction_len
    findings: list[Finding] = []
    if fraction_len > scale:
        findings.append(_error("bad_precision", f"more than {scale} digits after the decimal point"))
    if length is not None:
        before_limit = length - scale
        if before > before_limit:
            findings.append(
                _error(
                    "bad_precision",
                    f"more than {before_limit} digits before the decimal point in {length},{scale}",
                )
            )
        if total > length:
            findings.append(_error("bad_precision", f"more than {length} digits in total"))
    if not findings:
        findings.extend(_decimal_style(value, ip, fp))
    return findings


def _split_decimal(value: str) -> tuple[str, str, str | None]:
    body = value[1:] if value[0] == "-" else value
    sign = "-" if value[0] == "-" else ""
    if "." in body:
        ip, fp = body.split(".", 1)
        return sign, ip, fp
    return sign, body, None


def _decimal_style(value: str, ip: str, fp: str | None) -> list[Finding]:
    if value in VALID_DECIMAL_ZEROS:
        return []
    findings: list[Finding] = []
    if len(ip) > 1 and ip.startswith("0"):
        findings.append(_warn("leading_zero", "leading zero"))
    if fp is not None and fp.endswith("0"):
        findings.append(_warn("trailing_zero", "trailing zero"))
    return findings


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


def _valid_clock(value: str, picture: str) -> bool:
    width = {"%H%M": 4, "%H%M%S": 6}[picture]
    if not re.fullmatch(rf"\d{{{width}}}", value):
        return False
    try:
        dt.datetime.strptime(value, picture)
    except ValueError:
        return False
    return True
