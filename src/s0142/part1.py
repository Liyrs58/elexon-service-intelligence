"""File-format rules taken from NETA IDD Part 1 v64.0.

The local HTML copy is not committed. Its SHA-256 is recorded here so a
later reader can check the same file. Section numbers below are that
document. Wording here is a description of the rules, not a quotation.

Part 1 is the common flow format. It does not say that a P114 portal
download is byte-identical to a flow file.
"""

from __future__ import annotations

import re

PART1_SHA256 = "2530cdcc980329d41466c043eeb1f0b776b3fdcf49f6310f1bf4bf057078d547"
PART1_DOCUMENT = "NETA IDD Part 1 v64.0 (effective 2026-06-25)"
PART1_SECTIONS = "2.2.1, 2.2.2, 2.2.3, 2.2.11.24, 2.2.11.27, 2.2.11.30"

# Section 2.2.3 character table, stored as code points.
TEXT_CHARS = frozenset(
    {
        32,
        33,
        34,
        35,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        *range(48, 58),
        58,
        59,
        61,
        63,
        64,
        *range(65, 91),
        91,
        92,
        93,
        94,
        95,
        *range(97, 123),
        123,
        125,
    }
)

# Section 2.2.3 lists these eight spellings of zero. A decimal point with
# no digit is not one of them. The document's minus sign is the ASCII hyphen.
VALID_DECIMAL_ZEROS = frozenset(
    {
        "0",
        "0.0",
        ".0",
        "0.",
        "-0",
        "-0.0",
        "-.0",
        "-0.",
    }
)

# Section 2.2.11. The letters and codes are the set members.
RUN_TYPES = frozenset({"II", "SF", "R1", "R2", "R3", "RF", "D", "DF"})
PRICE_DERIVATION_CODES = frozenset(set("ABCDEFGHIJKLNP"))
PC_FLAGS = frozenset({"P", "C"})
VALID_SETS = {
    "Run Type": RUN_TYPES,
    "Price Derivation Code": PRICE_DERIVATION_CODES,
    "P/C Flag": PC_FLAGS,
}

_FILE_TYPE = re.compile(r"^S0142(\d{3})$")
_AAA_FIELDS = (
    "file_type",
    "message_role",
    "creation_time",
    "from_role_code",
    "from_participant_id",
    "to_role_code",
    "to_participant_id",
    "sequence_number",
    "test_data_flag",
)


def xor_record(checksum: int, record: bytes) -> int:
    """Fold one record into a 32-bit checksum.

    Section 2.2.2: walk the record without its line ending, in 4-byte
    big-endian chunks, pad a short chunk with zero bytes, XOR into the
    running total. The trailer is not included; the caller leaves it out.
    """

    n = len(record)
    i = 0
    checksum &= 0xFFFFFFFF
    while i < n:
        value = 0
        for _ in range(4):
            if i < n:
                value = ((value << 8) | record[i]) & 0xFFFFFFFF
                i += 1
            else:
                value = (value << 8) & 0xFFFFFFFF
                i += 1
        checksum ^= value
    return checksum


def checksum_records(records: list[bytes]) -> int:
    total = 0
    for record in records:
        total = xor_record(total, record)
    return total


def file_type_version(file_type: str) -> str | None:
    match = _FILE_TYPE.fullmatch(file_type)
    if match is None:
        return None
    return match.group(1)


def aaa_line(version: str, *, test_flag: str = "OPER") -> str:
    """Synthetic header. Ten fields, pipe after the last field."""

    fields = [
        "AAA",
        f"S0142{version}",
        "D",
        "20260101000000",
        "SA",
        "SYNTH001",
        "BP",
        "SYNTH001",
        "1",
        test_flag,
    ]
    return "|".join(fields) + "|"


def seal(lines: list[str]) -> list[str]:
    """Append a ZZZ whose count and checksum match these records."""

    payload = [line for line in lines if not line.startswith("ZZZ|")]
    total = 0
    for line in payload:
        total = xor_record(total, line.encode("ascii"))
    count = len(payload) + 1
    return payload + [f"ZZZ|{count}|{total}|"]
