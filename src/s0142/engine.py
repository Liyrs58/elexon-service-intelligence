"""Parse an S0142 file and load it into DuckDB, recording structural defects.

Framing, the AAA header, the ZZZ count and checksum, and the lexical pictures
come from NETA IDD Part 1 v64.0. See ``s0142.part1``.
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

from s0142.part1 import VALID_SETS, file_type_version, xor_record
from s0142.rules import DELETED_ACCEPTANCE, DELETION_VALUE_FIELDS
from s0142.specio import Spec, VersionLayout, load_spec
from s0142.typesys import Finding, ParsedType, check_lexical

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Defect:
    code: str
    line: int | None
    field_index: int | None
    record_type: str | None
    field_id: str | None
    message: str
    severity: str = "error"

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
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
    strict: bool = False

    @property
    def errors(self) -> list[Defect]:
        return [item for item in self.defects if item.severity != "warning"]

    @property
    def warnings(self) -> list[Defect]:
        return [item for item in self.defects if item.severity == "warning"]

    @property
    def ok(self) -> bool:
        if self.errors:
            return False
        if self.strict and self.warnings:
            return False
        return True

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "declared_version": self.declared_version,
            "layout_version": self.layout_version,
            "body_records": self.body_records,
            "strict": self.strict,
            "ok": self.ok,
            "defects": [item.to_json() for item in self.errors],
            "warnings": [item.to_json() for item in self.warnings],
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


@dataclass(frozen=True)
class Physical:
    line_no: int
    raw: bytes
    parts: list[str]
    crlf: bool
    missing_lf: bool
    terminal_pipe: bool
    ascii_ok: bool

    @property
    def record_type(self) -> str:
        if not self.parts:
            return ""
        return self.parts[0]


def _values(record: Physical) -> list[str]:
    """Fields after the record type.

    Part 1 puts a pipe after the last field, so a well-formed record ends
    with an empty split piece. That piece is not a field. A missing terminal
    pipe is reported separately and the last piece is kept.
    """

    values = record.parts[1:]
    if record.terminal_pipe and values and values[-1] == "":
        values = values[:-1]
    return values


def _iter_physical(path: Path, defects: list[Defect]):
    with path.open("rb") as handle:
        prefix = handle.read(3)
        if prefix == b"\xef\xbb\xbf":
            defects.append(
                Defect(
                    "framing",
                    1,
                    None,
                    None,
                    None,
                    "The file starts with a UTF-8 BOM. IDD Part 1 files are ASCII.",
                )
            )
        else:
            handle.seek(0)
        line_no = 0
        while True:
            blob = handle.readline()
            if not blob:
                break
            line_no += 1
            missing_lf = not blob.endswith(b"\n")
            if blob.endswith(b"\n"):
                blob = blob[:-1]
            crlf = blob.endswith(b"\r")
            if crlf:
                blob = blob[:-1]
            try:
                text = blob.decode("ascii")
                ascii_ok = True
            except UnicodeDecodeError:
                text = ""
                ascii_ok = False
            parts = text.split("|") if ascii_ok else []
            yield Physical(
                line_no=line_no,
                raw=blob,
                parts=parts,
                crlf=crlf,
                missing_lf=missing_lf,
                terminal_pipe=blob.endswith(b"|"),
                ascii_ok=ascii_ok,
            )


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
            defect_count BIGINT,
            warning_count BIGINT
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
            message VARCHAR,
            severity VARCHAR
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
        "INSERT INTO file_info VALUES (?, ?, ?, ?, ?, ?, ?)",
        [None, None, layout.version, sha256, 0, 0, 0],
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


def load_file(
    path: str | Path,
    database: str = ":memory:",
    version: str | None = None,
    spec: Spec | None = None,
    strict: bool = False,
) -> tuple[Report, duckdb.DuckDBPyConnection]:
    spec = spec or load_spec()
    path = Path(path)
    defects: list[Defect] = []
    signals: list[DeletionSignal] = []
    pending = _iter_physical(path, defects)
    try:
        first = next(pending)
    except StopIteration:
        first = None
    declared = _peek_version(first)
    aaa_line = first.line_no if first is not None and first.record_type == "AAA" else None

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

    records: list[Physical] | None = None
    if layout_version is None:
        records = [] if first is None else [first]
        records.extend(pending)
        if aaa_line is None:
            for record in records:
                if record.record_type == "AAA":
                    aaa_line = record.line_no
                    declared = _peek_version(record)
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
    scratch = tempfile.TemporaryDirectory(prefix="s0142-load-")
    writers: dict[str, tuple] = {}
    checksum = 0
    physical_count = 0
    last_record: Physical | None = None
    saw_aaa = False
    zzz_count = 0

    def _emit(table: str, row: tuple) -> None:
        slot = writers.get(table)
        if slot is None:
            csv_path = Path(scratch.name) / f"{table}.csv"
            handle = csv_path.open("w", encoding="utf-8", newline="")
            slot = (handle, csv.writer(handle), csv_path)
            writers[table] = slot
        slot[1].writerow(row)

    def close_until(level: int) -> None:
        while stack[-1].level >= level:
            _check_one_cardinality(defects, layout, stack.pop())

    for record in _records():
        physical_count += 1
        checksum = xor_record(checksum, record.raw)
        last_record = record
        _framing_defects(defects, record)
        if not record.ascii_ok or record.raw == b"":
            continue
        record_type = record.record_type
        if record_type == "AAA":
            saw_aaa = True
            if physical_count != 1:
                defects.append(
                    Defect("framing", record.line_no, None, "AAA", None, "AAA is not the first record.")
                )
            defects.extend(_aaa_fields(record))
            continue
        if record_type == "ZZZ":
            zzz_count += 1
            if zzz_count > 1:
                defects.append(Defect("framing", record.line_no, None, "ZZZ", None, "More than one ZZZ trailer."))
            continue
        body_records += 1
        if layout is None:
            continue
        group = layout.by_id.get(record_type)
        values = _values(record)
        if group is None:
            matches = _matching_versions(spec, record_type, len(values))
            if matches and layout_version not in matches:
                defects.append(
                    Defect(
                        "version_mismatch",
                        record.line_no,
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
                        record.line_no,
                        None,
                        record_type,
                        None,
                        f"Record type {record_type} is not in S0142 layout {layout_version}.",
                    )
                )
            continue

        if len(values) != group.field_count:
            matches = _matching_versions(spec, record_type, len(values))
            if matches and layout_version not in matches:
                defects.append(
                    Defect(
                        "version_mismatch",
                        record.line_no,
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
                        record.line_no,
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
            _reject_parent(defects, group, opener, record.line_no, by_id=None)
            opener.child_counts[record_type] = opener.child_counts.get(record_type, 0) + 1
            parent_line = opener.line or None
            _emit("record_index", (record.line_no, record_type, parent_line, len(values)))
            stack.append(_Open(record_type, record.line_no, group.level, {}))
            continue

        by_id = {item.id: values[item.order] for item in group.fields}
        close_until(group.level)
        opener = stack[-1]
        parent_line = opener.line or None
        parent_ok = _reject_parent(defects, group, opener, record.line_no, by_id=by_id)
        _check_fields(defects, signals, group, by_id, record.line_no, parent_ok)
        opener.child_counts[record_type] = opener.child_counts.get(record_type, 0) + 1
        stack.append(_Open(record_type, record.line_no, group.level, {}))
        _emit("record_index", (record.line_no, record_type, parent_line, len(values)))
        row = (record.line_no, parent_line, *[by_id[item.id] if by_id[item.id] != "" else None for item in group.fields])
        _emit("rec_" + group.id.lower(), row)

    if not saw_aaa:
        defects.append(Defect("framing", None, None, "AAA", None, "AAA header is missing."))
    if last_record is not None and last_record.record_type == "ZZZ" and last_record.ascii_ok:
        checksum = xor_record(checksum, last_record.raw)
        defects.extend(_zzz_fields(last_record, physical_count, checksum))
    else:
        defects.append(Defect("framing", None, None, "ZZZ", None, "ZZZ trailer is missing."))
    if zzz_count and (last_record is None or last_record.record_type != "ZZZ"):
        defects.append(Defect("framing", None, None, "ZZZ", None, "ZZZ is not the last record."))

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
                    (
                        item.code,
                        item.line,
                        item.field_index,
                        item.record_type,
                        item.field_id,
                        item.message,
                        item.severity,
                    )
                    for item in defects
                ],
            )
            _copy_rows(
                connection,
                "deletion_signals",
                [(item.line, item.record_type, json.dumps(list(item.value_fields))) for item in signals],
            )
            error_count = sum(1 for item in defects if item.severity != "warning")
            warning_count = sum(1 for item in defects if item.severity == "warning")
            connection.execute(
                """
                UPDATE file_info
                SET path = ?, declared_version = ?, layout_version = ?, body_records = ?,
                    defect_count = ?, warning_count = ?
                """,
                [str(path), declared, layout_version, body_records, error_count, warning_count],
            )
    finally:
        scratch.cleanup()

    report = Report(
        path=str(path),
        declared_version=declared,
        layout_version=layout_version,
        body_records=body_records,
        defects=defects,
        deletion_signals=signals,
        unresolved_ids=[item["id"] for item in spec.unresolved],
        strict=strict,
    )
    return report, connection


def _peek_version(record: Physical | None) -> str | None:
    if record is None or record.record_type != "AAA" or not record.ascii_ok:
        return None
    values = _values(record)
    if not values:
        return None
    return file_type_version(values[0])


def _framing_defects(defects: list[Defect], record: Physical) -> None:
    if not record.ascii_ok:
        defects.append(
            Defect(
                "framing",
                record.line_no,
                None,
                None,
                None,
                "Record is not ASCII. IDD Part 1 files use the ASCII character set.",
            )
        )
        return
    if record.crlf or b"\r" in record.raw:
        defects.append(
            Defect(
                "framing",
                record.line_no,
                None,
                record.record_type or None,
                None,
                "Record contains CR. Part 1 delimits records with a line feed only.",
            )
        )
    if record.missing_lf:
        defects.append(
            Defect(
                "framing",
                record.line_no,
                None,
                record.record_type or None,
                None,
                "Record does not end with a line feed.",
            )
        )
    if record.raw == b"":
        defects.append(Defect("framing", record.line_no, None, None, None, "Empty record."))
        return
    if not record.terminal_pipe:
        defects.append(
            Defect(
                "framing",
                record.line_no,
                None,
                record.record_type or None,
                None,
                "Record has no pipe after the last field.",
            )
        )


def _aaa_fields(record: Physical) -> list[Defect]:
    from s0142.typesys import parse_type

    values = _values(record)
    found: list[Defect] = []
    version = file_type_version(values[0]) if values else None
    if len(values) != 9:
        found.append(
            Defect(
                "framing",
                record.line_no,
                None,
                "AAA",
                None,
                f"AAA has {len(values) + 1} fields; Part 1 defines 10, with a pipe after the last field.",
            )
        )
        if version is None:
            found.append(
                Defect(
                    "version_mismatch",
                    record.line_no,
                    1,
                    "AAA",
                    "file_type",
                    "AAA file type is not S0142 plus a three-digit version.",
                )
            )
        return found
    file_type, role, created, from_role, from_id, to_role, to_id, sequence, test_flag = values
    if version is None:
        found.append(
            Defect(
                "version_mismatch",
                record.line_no,
                2,
                "AAA",
                "file_type",
                f"AAA file type {file_type!r} is not S0142 plus a three-digit version.",
            )
        )
    else:
        _apply(found, check_lexical(file_type, parse_type("text(8)")), record.line_no, 2, "AAA", "file_type")
    if role not in {"D", "R"}:
        found.append(
            Defect(
                "bad_type",
                record.line_no,
                3,
                "AAA",
                "message_role",
                f"Message role {role!r} is not D or R.",
            )
        )
    _apply(found, check_lexical(created, parse_type("datetime")), record.line_no, 4, "AAA", "creation_time")
    for index, field_id, value, kind in (
        (5, "from_role_code", from_role, "text(2)"),
        (6, "from_participant_id", from_id, "text(8)"),
        (7, "to_role_code", to_role, "text(2)"),
        (8, "to_participant_id", to_id, "text(8)"),
    ):
        if value == "":
            found.append(
                Defect(
                    "missing_required",
                    record.line_no,
                    index,
                    "AAA",
                    field_id,
                    f"AAA {field_id} is empty.",
                )
            )
        else:
            _apply(found, check_lexical(value, parse_type(kind)), record.line_no, index, "AAA", field_id)
    if sequence == "":
        found.append(
            Defect(
                "missing_required",
                record.line_no,
                8,
                "AAA",
                "sequence_number",
                "AAA sequence number is empty.",
            )
        )
    else:
        _apply(found, check_lexical(sequence, parse_type("integer(9)")), record.line_no, 8, "AAA", "sequence_number")
    if test_flag != "":
        _apply(found, check_lexical(test_flag, parse_type("text(4)")), record.line_no, 9, "AAA", "test_data_flag")
    return found


def _zzz_fields(record: Physical, physical_count: int, checksum: int) -> list[Defect]:
    from s0142.typesys import parse_type

    values = _values(record)
    found: list[Defect] = []
    if len(values) != 2:
        found.append(
            Defect(
                "framing",
                record.line_no,
                None,
                "ZZZ",
                None,
                f"ZZZ has {len(values) + 1} fields; Part 1 defines 3, with a pipe after the last field.",
            )
        )
    integer = parse_type("integer(10)")
    if len(values) >= 1 and values[0] != "":
        findings = check_lexical(values[0], integer)
        if any(item.severity == "error" for item in findings):
            _apply(found, findings, record.line_no, 1, "ZZZ", "record_count")
        else:
            if int(values[0]) != physical_count:
                found.append(
                    Defect(
                        "record_count",
                        record.line_no,
                        1,
                        "ZZZ",
                        "record_count",
                        f"ZZZ record count is {values[0]}; the file has {physical_count} records, including header and footer.",
                    )
                )
            _apply(
                found,
                [item for item in findings if item.severity == "warning"],
                record.line_no,
                1,
                "ZZZ",
                "record_count",
            )
    else:
        found.append(
            Defect("record_count", record.line_no, 1, "ZZZ", "record_count", "ZZZ record count is missing.")
        )
    if len(values) >= 2 and values[1] != "":
        findings = check_lexical(values[1], integer)
        if any(item.severity == "error" for item in findings):
            _apply(found, findings, record.line_no, 2, "ZZZ", "checksum")
        else:
            if int(values[1]) != checksum:
                found.append(
                    Defect(
                        "checksum",
                        record.line_no,
                        2,
                        "ZZZ",
                        "checksum",
                        f"ZZZ checksum is {values[1]}; the 32-bit XOR of the records before the trailer is {checksum}.",
                    )
                )
            _apply(
                found,
                [item for item in findings if item.severity == "warning"],
                record.line_no,
                2,
                "ZZZ",
                "checksum",
            )
    else:
        found.append(Defect("checksum", record.line_no, 2, "ZZZ", "checksum", "ZZZ checksum is missing."))
    return found


def _apply(defects, findings: list[Finding], line: int, index: int, record_type: str, field_id: str) -> None:
    for finding in findings:
        defects.append(
            Defect(
                finding.code,
                line,
                index,
                record_type,
                field_id,
                finding.detail,
                finding.severity,
            )
        )


def _infer_versions(spec: Spec, lines: list[Physical]) -> list[str]:
    observations: dict[str, set[int]] = {}
    for record in lines:
        if not record.ascii_ok:
            continue
        record_type = record.record_type
        if record_type in {"AAA", "ZZZ", ""}:
            continue
        observations.setdefault(record_type, set()).add(len(_values(record)))
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
    findings = list(check_lexical(value, _parsed_type(item)))
    if item.valid_set and not any(finding.severity == "error" for finding in findings):
        allowed = VALID_SETS.get(item.valid_set)
        if allowed is not None and value not in allowed:
            findings.append(
                Finding(
                    "warning",
                    "valid_set",
                    f"{value!r} is not a member of {item.valid_set}.",
                )
            )
    for finding in findings:
        if finding.code == "bad_precision":
            message = f"{item.id} value {value!r}: {finding.detail}. The limit is {item.data_type}."
        elif finding.detail:
            message = f"{item.id} value {value!r}: {finding.detail}."
        else:
            message = f"{item.id} value {value!r} does not match workbook type {item.data_type}."
        defects.append(
            Defect(
                finding.code,
                line_no,
                item.order + 1,
                record_type,
                item.id,
                message,
                finding.severity,
            )
        )


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
        f"defects: {len(report.errors)}",
        f"warnings: {len(report.warnings)}",
        f"strict: {report.strict}",
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
