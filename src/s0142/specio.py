"""Load the generated S0142 layout spec."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

WORKBOOK_SHA256 = "814c506592f4418bf981693969c7402db45020fedfb8b47573d431eacb23a117"
OFFICIAL_FILENAME = "NETA-IDD-Part-2_spreadsheet_v43.0.xls"

_DATA = Path(__file__).resolve().parent / "data" / "s0142_layout.json"


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    raw_type: str
    data_type: str
    kind: str
    length_parameter: int | None
    scale: int | None
    length_parameter_enforced: bool
    optional: bool
    valid_set: str | None
    comment: str
    level: int
    order: int
    workbook_row: int

    @classmethod
    def from_json(cls, payload: dict) -> "Field":
        return cls(
            id=payload["id"],
            name=payload["name"],
            raw_type=payload["raw_type"],
            data_type=payload["data_type"],
            kind=payload["kind"],
            length_parameter=payload["length_parameter"],
            scale=payload["scale"],
            length_parameter_enforced=payload["length_parameter_enforced"],
            optional=payload["optional"],
            valid_set=payload["valid_set"],
            comment=payload["comment"],
            level=payload["level"],
            order=payload["order"],
            workbook_row=payload["workbook_row"],
        )


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    range_text: str
    range_min: int
    range_max: int | None
    level: int
    parent_id: str | None
    comment: str
    workbook_row: int
    repeating: bool
    fields: tuple[Field, ...]

    @classmethod
    def from_json(cls, payload: dict) -> "Group":
        return cls(
            id=payload["id"],
            name=payload["name"],
            range_text=payload["range"],
            range_min=payload["range_min"],
            range_max=payload["range_max"],
            level=payload["level"],
            parent_id=payload["parent_id"],
            comment=payload["comment"],
            workbook_row=payload["workbook_row"],
            repeating=payload["repeating"],
            fields=tuple(Field.from_json(item) for item in payload["fields"]),
        )

    @property
    def field_count(self) -> int:
        return len(self.fields)


@dataclass(frozen=True)
class VersionLayout:
    version: str
    sheet_row_start: int
    sheet_row_end: int
    groups: tuple[Group, ...]
    by_id: dict[str, Group]

    @classmethod
    def from_json(cls, version: str, payload: dict) -> "VersionLayout":
        groups = tuple(Group.from_json(item) for item in payload["groups"])
        return cls(
            version=version,
            sheet_row_start=payload["sheet_row_start"],
            sheet_row_end=payload["sheet_row_end"],
            groups=groups,
            by_id={group.id: group for group in groups},
        )


@dataclass(frozen=True)
class Spec:
    payload: dict
    versions: dict[str, VersionLayout]

    @property
    def sha256(self) -> str:
        return self.payload["source"]["sha256"]

    @property
    def unresolved(self) -> list[dict]:
        return list(self.payload["unresolved"])

    @property
    def disagreements(self) -> list[dict]:
        return list(self.payload["disagreements"])


def spec_path() -> Path:
    return _DATA


@lru_cache(maxsize=1)
def load_spec(path: str | None = None) -> Spec:
    text = Path(path).read_text(encoding="utf-8") if path else _DATA.read_text(encoding="utf-8")
    payload = json.loads(text)
    versions = {
        version: VersionLayout.from_json(version, body)
        for version, body in payload["versions"].items()
    }
    return Spec(payload=payload, versions=versions)
