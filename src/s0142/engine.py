"""Parse an S0142 file and load it into DuckDB, recording structural defects.

Physical framing (pipe separator, AAA version token, ZZZ trailer) is not defined
on the S0142 rows of the workbook. Those choices are isolated in this module
and listed as UNRESOLVED in the spec.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from s0142.rules import DELETED_ACCEPTANCE, DELETION_VALUE_FIELDS
from s0142.specio import Spec, VersionLayout, load_spec
from s0142.typesys import ParsedType, check_lexical

_FILE_TYPE = re.compile(r"^S0142(\d{3})$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Defect:
    code: str
    line: int | None
    field_index: int | None
    record_type: str | None
    field_id: str | None
    message: str

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "line": self.line,
            "field_index": self.field_index,
            "record_type": self.record_type,
            "field_id": self.field_id,
            "message": self.message,
        }


@dataclass
class DeletionSignal:
    """A cited deletion encoding that satisfied its own rule. Not a defect."""

    line: int
    record_type: str
    value_fields: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "record_type": self.record_type,
            "value_fields": list(self.value_fields),
        }


@dataclass
class Report:
    path: str
    declared_version: str | None
    layout_version: str | None
    body_records: int
    defects: list[Defect] = field(default_factory=list)
    deletion_signals: list[DeletionSignal] = field(default_factory=list)
    unresolved_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.defects

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "declared_version": self.declared_version,
            "layout_version": self.layout_version,
            "body_records": self.body_records,
            "ok": self.ok,
            "defects": [item.to_json() for item in self.defects],
            "deletion_signals": [item.to_json() for item in self.deletion_signals],
            "unresolved_ids": self.unresolved_ids,
        }


@dataclass
class _Open:
    record_type: str
    line: int
    level: int
    child_counts: dict[str, int]


def _quote(identifier: str) -> str:
    if not _IDENT.fullmatch(identifier):
        raise ValueError(f"unsafe SQL identifier {identifier!r}")
    return f'"{identifier}"'


def _values(parts: list[str]) -> list[str]:
    """Fields after the record type.

    A terminal pipe is not a field: one trailing empty piece is removed.
    ``TYPE|a|b|`` and ``TYPE|a|b`` both yield ``['a', 'b']``. ``TYPE|a||``
    yields ``['a', '']``. A record whose last field is empty and which also
    omits the terminal pipe is indistinguishable from a shorter record.
    """

    values = parts[1:]
    if values and values[-1] == "":
        values = values[:-1]
    return values


def _version_from_aaa(parts: list[str]) -> str | None:
    values = _values(parts)
    if not values:
        return None
    match = _FILE_TYPE.fullmatch(values[0])
    if match:
        return match.group(1)
    if len(values) >= 2 and values[0] == "S0142" and re.fullmatch(r"\d{3}", values[1]):
        return values[1]
    return None


def _matching_versions(spec: Spec, record_type: str, count: int) -> list[str]:
    found = []
    for version, layout in spec.versions.items():
        group = layout.by_id.get(record_type)
        if group is not None and group.field_count == count:
            found.append(version)
    return found


def _parsed_type(field) -> ParsedType:
    return ParsedType(
        raw=field.raw_type,
        data_type=field.data_type,
        kind=field.kind,
        length_parameter=field.length_parameter,
        scale=field.scale,
        length_parameter_enforced=field.length_parameter_enforced,
    )


def _create_schema(connection, layout: VersionLayout, sha256: str) -> None:
    connection.execute(
        """
        CREATE TABLE file_info (
            path VARCHAR,
            declared_version VARCHAR,
            layout_version VARCHAR,
            workbook_sha256 VARCHAR,
            body_records BIGINT,
            defect_count BIGINT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE defects (
            code VARCHAR,
            line_no INTEGER,
            field_index INTEGER,
            record_type VARCHAR,
            field_id VARCHAR,
            message VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE deletion_signals (
            line_no INTEGER,
            record_type VARCHAR,
            value_fields VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE record_index (
            line_no INTEGER,
            record_type VARCHAR,
            parent_line_no INTEGER,
            field_count INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE layout_field (
            record_type VARCHAR,
            field_index INTEGER,
            field_id VARCHAR,
            field_name VARCHAR,
            data_type VARCHAR,
            optional BOOLEAN,
            parent_record VARCHAR,
            range_text VARCHAR
        )
        """
    )
    field_rows = []
    for group in layout.groups:
        for item in group.fields:
            field_rows.append(
                (
                    group.id,
                    item.order + 1,
                    item.id,
                    item.name,
                    item.data_type,
                    item.optional,
                    group.parent_id,
                    group.range_text,
                )
            )
        columns = ["line_no INTEGER", "parent_line_no INTEGER"]
        for item in group.fields:
            columns.append(f"{_quote(item.id.lower())} VARCHAR")
        connection.execute(f"CREATE TABLE {_quote('rec_' + group.id.lower())} ({', '.join(columns)})")
    _copy_rows(connection, "layout_field", field_rows)
    connection.execute(
        "INSERT INTO file_info VALUES (?, ?, ?, ?, ?, ?)",
        [None, None, layout.version, sha256, 0, 0],
    )


def _copy_rows(connection, table: str, rows) -> None:
    """Load rows with COPY. ``executemany`` on this DuckDB build is far too slow."""

    rows = list(rows)
    if not rows:
        return
    handle = tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", prefix=f"{table}-", suffix=".csv", delete=False
    )
    path = handle.name
    try:
        csv.writer(handle).writerows(rows)
        handle.close()
        connection.execute(f"COPY {_quote(table)} FROM ? (HEADER false, NULL '')", [path])
    finally:
        handle.close()
        os.unlink(path)


def _iter_records(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        first_physical = True
        for line_no, raw in enumerate(handle, start=1):
            text = raw.rstrip("\n").rstrip("\r")
            if first_physical:
                text = text.lstrip("\ufeff")
                first_physical = False
            if text.strip() == "":
                continue
            yield line_no, text.split("|")


def load_file(
    path: str | Path,
    database: str = ":memory:",
    version: str | None = None,
    spec: Spec | None = None,
) -> tuple[Report, duckdb.DuckDBPyConnection]:
    spec = spec or load_spec()
    path = Path(path)
    defects: list[Defect] = []
    signals: list[DeletionSignal] = []
    declared: str | None = None
    pending = _iter_records(path)
    try:
        first = next(pending)
    except StopIteration:
        first = None
    aaa_line = None
    if first is not None and first[1] and first[1][0] == "AAA":
        aaa_line = first[0]
        declared = _version_from_aaa(first[1])
        if declared is None:
            defects.append(
                Defect(
                    "version_mismatch",
                    aaa_line,
                    None,
                    "AAA",
                    None,
                    "AAA is present but its file type is not S0142 plus a three-digit layout version.",
                )
            )

    override = version
    if override is not None and override not in spec.versions:
        raise ValueError(f"unknown layout version {override}")
    if override is not None and declared is not None and override != declared:
        defects.append(
            Defect(
                "version_mismatch",
                aaa_line,
                None,
                "AAA",
                None,
                f"AAA declares layout {declared} and --version says {override}.",
            )
        )

    layout_version = override or declared
    if layout_version not in spec.versions:
        layout_version = None
    unknown_declaration = declared is not None and declared not in spec.versions and override is None
    if unknown_declaration:
        defects.append(
            Defect(
                "version_mismatch",
                aaa_line,
                None,
                "AAA",
                None,
                f"AAA declares layout {declared}, which is not in the workbook spec.",
            )
        )

    records: list[tuple[int, list[str]]] | None = None
    if layout_version is None:
        records = [] if first is None else [first]
        records.extend(pending)
        if aaa_line is None:
            for line_no, parts in records:
                if parts and parts[0] == "AAA":
                    aaa_line = line_no
                    declared = _version_from_aaa(parts)
                    if records and records[0][0] != aaa_line:
                        defects.append(
                            Defect(
                                "framing",
                                aaa_line,
                                None,
                                "AAA",
                                None,
                                "AAA is not the first record.",
                            )
                        )
                    if declared is None:
                        defects.append(
                            Defect(
                                "version_mismatch",
                                aaa_line,
                                None,
                                "AAA",
                                None,
                                "AAA is present but its file type is not S0142 plus a three-digit layout version.",
                            )
                        )
                    break
        compatible = _infer_versions(spec, records)
        if unknown_declaration:
            compatible = []
        if len(compatible) == 1 and layout_version is None and version is None:
            layout_version = compatible[0]
        elif layout_version is None and not unknown_declaration:
            shown = ", ".join(compatible) if compatible else "none"
            defects.append(
                Defect(
                    "version_mismatch",
                    1,
                    None,
                    None,
                    None,
                    f"Layout version could not be chosen. Compatible versions: {shown}.",
                )
            )

    def _records():
        if records is not None:
            yield from records
            return
        if first is not None:
            yield first
        yield from pending

    connection = duckdb.connect(database)
    layout = spec.versions[layout_version] if layout_version is not None else None
    if layout is not None:
        _create_schema(connection, layout, spec.sha256)

    root = _Open("", 0, 0, {})
    stack = [root]
    body_records = 0
    seen_trailer = False
    scratch = tempfile.TemporaryDirectory(prefix="s0142-load-")
    writers: dict[str, tuple] = {}

    def _emit(table: str, row: tuple) -> None:
        slot = writers.get(table)
        if slot is None:
            path = Path(scratch.name) / f"{table}.csv"
            handle = path.open("w", encoding="utf-8", newline="")
            slot = (handle, csv.writer(handle), path)
            writers[table] = slot
        slot[1].writerow(row)

    def close_until(level: int) -> None:
        while stack[-1].level >= level:
            _check_one_cardinality(defects, layout, stack.pop())

    for line_no, parts in _records():
        record_type = parts[0] if parts else ""
        if record_type in {"AAA", "ZZZ"}:
            if record_type == "ZZZ" and seen_trailer:
                defects.append(Defect("framing", line_no, None, "ZZZ", None, "More than one ZZZ trailer."))
            if record_type == "ZZZ":
                seen_trailer = True
            if seen_trailer and record_type not in {"ZZZ"}:
                defects.append(Defect("framing", line_no, None, record_type, None, "Record follows the ZZZ trailer."))
            continue
        if seen_trailer:
            defects.append(Defect("framing", line_no, None, record_type, None, "Record follows the ZZZ trailer."))
            continue
        body_records += 1
        if layout is None:
            continue
        group = layout.by_id.get(record_type)
        if group is None:
            matches = _matching_versions(spec, record_type, len(_values(parts)))
            if matches and layout_version not in matches:
                defects.append(
                    Defect(
                        "version_mismatch",
                        line_no,
                        None,
                        record_type,
                        None,
                        f"{record_type} is not in layout {layout_version}; the field count fits {', '.join(matches)}.",
                    )
                )
            else:
                defects.append(
                    Defect(
                        "unknown_record",
                        line_no,
                        None,
                        record_type,
                        None,
                        f"Record type {record_type} is not in S0142 layout {layout_version}.",
                    )
                )
            continue

        values = _values(parts)
        if len(values) != group.field_count:
            matches = _matching_versions(spec, record_type, len(values))
            if matches and layout_version not in matches:
                defects.append(
                    Defect(
                        "version_mismatch",
                        line_no,
                        None,
                        record_type,
                        None,
                        (
                            f"{record_type} has {len(values)} fields; layout {layout_version} "
                            f"expects {group.field_count}. That count matches layout {', '.join(matches)}."
                        ),
                    )
                )
            else:
                defects.append(
                    Defect(
                        "field_count",
                        line_no,
                        None,
                        record_type,
                        None,
                        (
                            f"{record_type} has {len(values)} fields; layout {layout_version} "
                            f"expects {group.field_count}."
                        ),
                    )
                )
            close_until(group.level)
            opener = stack[-1]
            _reject_parent(defects, group, opener, line_no, by_id=None)
            opener.child_counts[record_type] = opener.child_counts.get(record_type, 0) + 1
            parent_line = opener.line or None
            _emit("record_index", (line_no, record_type, parent_line, len(values)))
            stack.append(_Open(record_type, line_no, group.level, {}))
            continue

        by_id = {item.id: values[item.order] for item in group.fields}
        close_until(group.level)
        opener = stack[-1]
        parent_line = opener.line or None
        parent_ok = _reject_parent(defects, group, opener, line_no, by_id=by_id)
        _check_fields(defects, signals, group, by_id, line_no, parent_ok)
        opener.child_counts[record_type] = opener.child_counts.get(record_type, 0) + 1
        stack.append(_Open(record_type, line_no, group.level, {}))
        _emit("record_index", (line_no, record_type, parent_line, len(values)))
        row = (line_no, parent_line, *[by_id[item.id] if by_id[item.id] != "" else None for item in group.fields])
        _emit("rec_" + group.id.lower(), row)

    while stack:
        _check_one_cardinality(defects, layout, stack.pop())

    try:
        if layout is not None:
            for handle, _writer, csv_path in writers.values():
                handle.close()
                connection.execute(
                    f"COPY {_quote(csv_path.stem)} FROM ? (HEADER false, NULL '')",
                    [str(csv_path)],
                )
            _copy_rows(
                connection,
                "defects",
                [
                    (item.code, item.line, item.field_index, item.record_type, item.field_id, item.message)
                    for item in defects
                ],
            )
            _copy_rows(
                connection,
                "deletion_signals",
                [(item.line, item.record_type, json.dumps(list(item.value_fields))) for item in signals],
            )
            connection.execute(
                "UPDATE file_info SET path = ?, declared_version = ?, layout_version = ?, body_records = ?, defect_count = ?",
                [str(path), declared, layout_version, body_records, len(defects)],
            )
    finally:
        scratch.cleanup()

    report = Report(
        path=str(path),
        declared_version=declared if override is None else override,
        layout_version=layout_version,
        body_records=body_records,
        defects=defects,
        deletion_signals=signals,
        unresolved_ids=[item["id"] for item in spec.unresolved],
    )
    if override is not None:
        report.declared_version = declared
    return report, connection


def _infer_versions(spec: Spec, lines: list[tuple[int, list[str]]]) -> list[str]:
    observations: dict[str, set[int]] = {}
    for _, parts in lines:
        record_type = parts[0]
        if record_type in {"AAA", "ZZZ"}:
            continue
        observations.setdefault(record_type, set()).add(len(_values(parts)))
    compatible = []
    for version, layout in spec.versions.items():
        ok = True
        for record_type, counts in observations.items():
            group = layout.by_id.get(record_type)
            if group is None or any(count != group.field_count for count in counts):
                ok = False
                break
        if ok:
            compatible.append(version)
    return compatible


def _reject_parent(defects, group, opener: _Open, line_no: int, by_id) -> bool:
    expected = group.parent_id
    actual = opener.record_type or None
    if expected == actual and opener.level == group.level - 1:
        return True
    defects.append(
        Defect(
            "parent_child",
            line_no,
            None,
            group.id,
            None,
            (
                f"{group.id} is nested under {expected or 'the file root'} in the workbook; "
                f"the open parent is {actual or 'the file root'}."
            ),
        )
    )
    if (
        by_id is not None
        and group.id == DELETED_ACCEPTANCE["child_record"]
        and _deletion_shape(group.id, by_id)
    ):
        defects.append(
            Defect(
                "omission_deletion",
                line_no,
                None,
                group.id,
                None,
                (
                    "BO6 value fields are empty, which is the cited deletion encoding, "
                    "but the open parent is not BO3. A deleted acceptance keeps BO3."
                ),
            )
        )
    return False


def _deletion_shape(record_type: str, by_id: dict[str, str]) -> bool:
    fields = DELETION_VALUE_FIELDS.get(record_type)
    if not fields:
        return False
    if any(field_id not in by_id for field_id in fields):
        return False
    return all(by_id[field_id] == "" for field_id in fields)


def _check_fields(defects, signals, group, by_id, line_no: int, parent_ok: bool) -> None:
    value_fields = DELETION_VALUE_FIELDS.get(group.id)
    deletion = bool(value_fields) and _deletion_shape(group.id, by_id)
    emitted_omission = False
    if deletion:
        for item in group.fields:
            value = by_id[item.id]
            if item.id in value_fields:
                if value == "":
                    continue
                _emit_lexical(defects, group.id, item, value, line_no)
                continue
            if value == "":
                if not item.optional:
                    emitted_omission = True
                    defects.append(
                        Defect(
                            "omission_deletion",
                            line_no,
                            item.order + 1,
                            group.id,
                            item.id,
                            (
                                f"{group.id} is in the deletion shape (empty value fields "
                                f"{', '.join(value_fields)}) but {item.id} is also empty. "
                                "The cited rule only empties those value fields."
                            ),
                        )
                    )
                continue
            _emit_lexical(defects, group.id, item, value, line_no)
        if parent_ok and not emitted_omission:
            signals.append(DeletionSignal(line_no, group.id, tuple(value_fields)))
        return

    for item in group.fields:
        value = by_id[item.id]
        if value == "":
            if not item.optional:
                defects.append(
                    Defect(
                        "missing_required",
                        line_no,
                        item.order + 1,
                        group.id,
                        item.id,
                        f"{item.id} is required in the workbook and is empty. Empty is not read as zero.",
                    )
                )
            continue
        _emit_lexical(defects, group.id, item, value, line_no)


def _emit_lexical(defects, record_type: str, item, value: str, line_no: int) -> None:
    code = check_lexical(value, _parsed_type(item))
    if code is None:
        return
    if code == "bad_precision":
        message = (
            f"{item.id} value {value!r} has more fraction digits than {item.data_type} allows. "
            "The limit is this field's scale, not a file-wide precision."
        )
    else:
        message = f"{item.id} value {value!r} does not match workbook type {item.data_type}."
    defects.append(Defect(code, line_no, item.order + 1, record_type, item.id, message))


_CHILDREN: dict[int, dict] = {}


def _children_index(layout: VersionLayout) -> dict:
    cached = _CHILDREN.get(id(layout))
    if cached is None:
        cached = {}
        for group in layout.groups:
            cached.setdefault(group.parent_id, []).append(group)
        _CHILDREN[id(layout)] = cached
    return cached


def _check_one_cardinality(defects, layout: VersionLayout | None, occurrence: _Open) -> None:
    if layout is None:
        return
    parent_key = None if occurrence.record_type == "" else occurrence.record_type
    if parent_key is not None and parent_key not in layout.by_id:
        return
    for child in _children_index(layout).get(parent_key, ()):
        count = occurrence.child_counts.get(child.id, 0)
        upper = child.range_max
        if count < child.range_min or (upper is not None and count > upper):
            upper_text = "*" if upper is None else str(upper)
            where = "the file" if occurrence.line == 0 else f"line {occurrence.line} ({occurrence.record_type})"
            defects.append(
                Defect(
                    "cardinality",
                    occurrence.line or None,
                    None,
                    child.id,
                    None,
                    (
                        f"{child.id} occurs {count} time(s) under {where}; "
                        f"the workbook range is {child.range_min}-{upper_text}."
                    ),
                )
            )


def render_text(report: Report) -> str:
    declared = report.declared_version or "(none)"
    chosen = report.layout_version or "(none)"
    lines = [
        f"path: {report.path}",
        f"declared_version: {declared}",
        f"layout_version: {chosen}",
        f"body_records: {report.body_records}",
        f"defects: {len(report.defects)}",
        f"deletion_signals: {len(report.deletion_signals)}",
        "unresolved: " + ", ".join(report.unresolved_ids),
    ]
    for item in report.defects:
        location = f"line {item.line}" if item.line else "file"
        if item.field_index:
            location += f" field {item.field_index}"
        if item.record_type:
            location += f" {item.record_type}"
        if item.field_id:
            location += f" {item.field_id}"
        lines.append(f"{item.code}: {location}: {item.message}")
    return "\n".join(lines) + "\n"
