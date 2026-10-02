"""Synthetic S0142 files. No real settlement data is produced here."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from s0142.rules import DELETION_VALUE_FIELDS
from s0142.specio import Spec, load_spec

PARTY_ID = "SYNTH001"
BM_UNIT_ID = "SYNTHBMU001"


@dataclass(frozen=True)
class _Ctx:
    period: int = 1
    record_type: str = ""


def _sample(field, ctx: _Ctx) -> str:
    if field.id == "N0316" and field.optional:
        return ""
    if field.id == "N0201":
        return str(ctx.period)
    if field.id == "N0202":
        return "II"
    if field.id == "N0045":
        return PARTY_ID
    if field.id == "N0034":
        return BM_UNIT_ID
    if (
        ctx.record_type == "APC"
        and field.order == 0
        and field.kind == "decimal"
        and field.scale is not None
    ):
        return "-1." + ("0" * field.scale) if field.scale else "-1"
    kind = field.kind
    if kind == "date":
        return "20260101"
    if kind == "datetime":
        return "20260101000000"
    if kind == "boolean":
        return "T"
    if kind == "char":
        return "P"
    if kind in {"text", "varchar2"}:
        token = "S"
        if field.length_parameter is not None and len(token) > field.length_parameter:
            raise RuntimeError(f"{field.id} cannot hold a synthetic token")
        return token
    if kind == "integer" or (kind == "number" and field.scale == 0):
        return "1"
    if kind in {"decimal", "number"} and field.scale is not None:
        if field.scale == 0:
            return "1"
        return "1." + ("0" * field.scale)
    raise RuntimeError(f"no synthetic value for {field.id} {field.data_type}")


def _line(group, ctx: _Ctx) -> str:
    ctx = replace(ctx, record_type=group.id)
    values = [_sample(item, ctx) for item in group.fields]
    for item, value in zip(group.fields, values, strict=True):
        if item.kind in {"text", "varchar2"} and item.length_parameter is not None:
            if len(value) > item.length_parameter:
                raise RuntimeError(f"{group.id}.{item.id} synthetic value {value!r} exceeds {item.data_type}")
    return group.id + "|" + "|".join(values) + "|"


def _children(layout, parent_id: str | None):
    return [group for group in layout.groups if group.parent_id == parent_id]


def _deletion_fp2(layout) -> str | None:
    fp2 = layout.by_id.get("FP2")
    if fp2 is None or "BP7" not in layout.by_id:
        return None
    values = []
    for item in fp2.fields:
        if item.id in DELETION_VALUE_FIELDS["FP2"]:
            values.append("")
        elif item.id == "N0220":
            values.append("0")
        elif item.id == "N0221":
            values.append("30")
        else:
            values.append(_sample(item, _Ctx(record_type="FP2")))
    return "FP2|" + "|".join(values) + "|"


def valid_lines(version: str, spec: Spec | None = None) -> list[str]:
    spec = spec or load_spec()
    layout = spec.versions[version]
    lines = [f"AAA|S0142{version}|"]

    def emit(parent_id: str | None, ctx: _Ctx) -> None:
        for child in _children(layout, parent_id):
            count = child.range_min
            if count == 0:
                continue
            for index in range(count):
                child_ctx = ctx
                if child.id in {"SPI", "SP7"}:
                    child_ctx = replace(ctx, period=index + 1)
                lines.append(_line(child, child_ctx))
                emit(child.id, child_ctx)
                if child.id == "SP7" and child_ctx.period == 1 and "BP7" in layout.by_id:
                    lines.append(_line(layout.by_id["BP7"], child_ctx))
                    deletion = _deletion_fp2(layout)
                    if deletion is not None:
                        lines.append(deletion)

    emit(None, _Ctx())
    lines.append("ZZZ|")
    return lines


def render_valid(version: str, spec: Spec | None = None) -> str:
    return "\n".join(valid_lines(version, spec)) + "\n"


def _mutate_field_count(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if line.startswith("SRH|"):
            parts = line.split("|")
            # parts[-1] is empty because of the terminal pipe. Drop the last real field.
            del parts[-2]
            line = "|".join(parts)
        out.append(line)
    return out


def _mutate_bad_type(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if line.startswith("SRH|"):
            parts = line.split("|")
            parts[1] = "NOTADATE"
            line = "|".join(parts)
        out.append(line)
    return out


def _mutate_bad_precision(lines: list[str], spec: Spec) -> list[str]:
    group = spec.versions["014"].by_id["BP7"]
    index = next(item.order for item in group.fields if item.id == "N0676")
    out = []
    done = False
    for line in lines:
        if not done and line.startswith("BP7|"):
            parts = line.split("|")
            # parts[0] is the record type; field order 0 is parts[1].
            parts[index + 1] = "1.2345"
            line = "|".join(parts)
            done = True
        out.append(line)
    if not done:
        raise RuntimeError("valid v014 file has no BP7 row to break")
    return out


def _insert_after(lines: list[str], prefix: str, extra: str) -> list[str]:
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith(prefix):
            out.append(extra)
            inserted = True
    if not inserted:
        raise RuntimeError(f"no line starts with {prefix}")
    return out


def _mutate_version(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if line.startswith("AAA|"):
            line = line.replace("S0142014", "S0142013", 1)
        out.append(line)
    return out


def _mutate_omission(lines: list[str]) -> list[str]:
    out = []
    done = False
    for line in lines:
        if not done and line.startswith("FP2|"):
            # Four empty fields and a terminal pipe. ``FP2||||`` is one field short
            # under the terminal-pipe rule.
            line = "FP2|||||"
            done = True
        out.append(line)
    if not done:
        raise RuntimeError("valid v014 file has no FP2 row")
    return out


def defect_lines(kind: str, spec: Spec | None = None) -> list[str]:
    spec = spec or load_spec()
    base = valid_lines("014", spec)
    if kind == "field_count":
        return _mutate_field_count(base)
    if kind == "bad_type":
        return _mutate_bad_type(base)
    if kind == "bad_precision":
        return _mutate_bad_precision(base, spec)
    if kind == "unknown_record":
        return _insert_after(base, "AAA|", "XYZ|1|")
    if kind == "parent_child":
        return _insert_after(base, "SRH|", "MEL|0|1.000|30|1.000|")
    if kind == "version_mismatch":
        return _mutate_version(base)
    if kind == "omission_deletion":
        return _mutate_omission(base)
    raise ValueError(kind)


DEFECT_KINDS = (
    "field_count",
    "bad_type",
    "bad_precision",
    "unknown_record",
    "parent_child",
    "version_mismatch",
    "omission_deletion",
)


def write_fixtures(directory: Path, spec: Spec | None = None) -> None:
    spec = spec or load_spec()
    directory.mkdir(parents=True, exist_ok=True)
    for version in spec.versions:
        (directory / f"valid_v{version}.txt").write_text(render_valid(version, spec), encoding="utf-8")
    for kind in DEFECT_KINDS:
        text = "\n".join(defect_lines(kind, spec)) + "\n"
        (directory / f"defect_{kind}.txt").write_text(text, encoding="utf-8")


def write_benchmark_file(path: Path, body_records: int = 1_000_000, version: str = "014") -> int:
    """Write a valid file whose body contains ``body_records`` records.

    The skeleton is the valid fixture. Extra MEL rows sit under the first BPI.
    MEL's workbook range is ``0-*``.
    """

    spec = load_spec()
    skeleton = valid_lines(version, spec)
    if "BPI" not in spec.versions[version].by_id or "MEL" not in spec.versions[version].by_id:
        raise RuntimeError(f"layout {version} has no BPI/MEL pair to pad")
    body = [line for line in skeleton if not line.startswith(("AAA|", "ZZZ|"))]
    extra = body_records - len(body)
    if extra < 0:
        raise RuntimeError(f"skeleton already has {len(body)} body records")
    mel = "MEL|0|1.000|30|1.000|"
    path.parent.mkdir(parents=True, exist_ok=True)
    written_body = 0
    padded = False
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in skeleton:
            if line.startswith("ZZZ|"):
                if not padded:
                    raise RuntimeError("no BPI row found to attach MEL rows")
                handle.write(line + "\n")
                continue
            handle.write(line + "\n")
            if not line.startswith("AAA|"):
                written_body += 1
            if not padded and line.startswith("BPI|"):
                for _ in range(extra):
                    handle.write(mel + "\n")
                written_body += extra
                padded = True
    return written_body
