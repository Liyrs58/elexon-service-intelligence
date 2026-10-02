"""Extract S0142 layouts from the NETA IDD Part 2 workbook.

The workbook is BIFF .xls. Reading it needs the optional ``xlrd`` dependency
(``pip install 's0142[extract]'``). Runtime parsing of settlement files does
not import xlrd.

Extraction walks the SO sheet. It does not hand-copy field lists.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from s0142.rules import as_json as rules_json
from s0142.specio import OFFICIAL_FILENAME, WORKBOOK_SHA256
from s0142.typesys import parse_type

_FLOW_ID = "S0142"
_LEVEL_COLUMNS = range(3, 12)  # L1..L9


class ExtractionError(RuntimeError):
    pass


def _norm_version(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if text.isdigit():
        return text.zfill(3)
    return text


def _parse_range(value) -> tuple[int, int | None, str]:
    if isinstance(value, float) and value.is_integer():
        number = int(value)
        return number, number, str(number)
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        number = int(text[:-2])
        return number, number, text
    if re.fullmatch(r"\d+", text):
        number = int(text)
        return number, number, text
    pair = re.fullmatch(r"(\d+)-(\d+)", text)
    if pair:
        return int(pair.group(1)), int(pair.group(2)), text
    unbounded = re.fullmatch(r"(\d+)-\*", text)
    if unbounded:
        return int(unbounded.group(1)), None, text
    raise ExtractionError(f"unrecognised group range {value!r}")


def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _open_workbook(path: Path):
    try:
        import xlrd.book as book
        import xlrd.formula as formula
    except ImportError as exc:
        raise ExtractionError(
            "Reading the workbook needs xlrd. Install it with: pip install 's0142[extract]'"
        ) from exc

    def safe(bk, nobj, namex, blah=0, level=0):
        try:
            return formula.evaluate_name_formula(bk, nobj, namex, blah=blah, level=level)
        except Exception:
            nobj.evaluated = 1
            nobj.any_err = 1
            return None

    book.evaluate_name_formula = safe
    import xlrd

    sink = StringIO()
    with redirect_stdout(sink), redirect_stderr(sink):
        return xlrd.open_workbook(str(path), logfile=sink)


def _level_mark(sheet, row: int):
    found = None
    for column in _LEVEL_COLUMNS:
        value = sheet.cell_value(row, column)
        if value == "" or value is None:
            continue
        if found is not None:
            raise ExtractionError(f"row {row} has more than one level mark")
        found = (column - 2, value)  # L1 == 1
    return found


def _option(mark, row: int) -> bool:
    if mark == "O":
        return True
    if mark == 1 or mark == 1.0 or mark == "1" or mark == "1.0":
        return False
    raise ExtractionError(f"row {row} has unrecognised optionality mark {mark!r}")


def _flow_spans(sheet) -> list[tuple[str, int, int]]:
    starts: list[tuple[str, int]] = []
    for row in range(sheet.nrows):
        flow_id = _cell_str(sheet.cell_value(row, 0))
        kind = _cell_str(sheet.cell_value(row, 1))
        if flow_id == _FLOW_ID and kind == "F":
            starts.append((_norm_version(sheet.cell_value(row, 2)), row))
    if not starts:
        raise ExtractionError("SO sheet has no S0142 flow rows")
    spans = []
    for index, (version, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else sheet.nrows
        for row in range(start + 1, end):
            if _cell_str(sheet.cell_value(row, 1)) == "F":
                end = row
                break
        spans.append((version, start, end))
    return spans


def _parse_version(sheet, version: str, start: int, end: int) -> dict:
    flow_name = _cell_str(sheet.cell_value(start, 14))
    groups: list[dict] = []
    stack: list[tuple[int, str]] = []
    current: dict | None = None
    for row in range(start + 1, end):
        item_id = _cell_str(sheet.cell_value(row, 0))
        kind = _cell_str(sheet.cell_value(row, 1))
        if kind == "" and item_id == "":
            continue
        if kind not in {"R", "D"}:
            raise ExtractionError(f"row {row} has unexpected type {kind!r}")
        located = _level_mark(sheet, row)
        if located is None:
            raise ExtractionError(f"row {row} ({item_id}) has no level mark")
        level, mark = located
        name = str(sheet.cell_value(row, 14)).strip()
        comment = str(sheet.cell_value(row, 15)).strip()
        if kind == "R":
            if mark != "G":
                raise ExtractionError(f"row {row} group {item_id} mark is {mark!r}, expected G")
            while stack and stack[-1][0] >= level:
                stack.pop()
            if level == 1:
                parent = None
            else:
                if not stack or stack[-1][0] != level - 1:
                    raise ExtractionError(
                        f"row {row} group {item_id} at L{level} has no parent at L{level - 1}"
                    )
                parent = stack[-1][1]
            range_min, range_max, range_text = _parse_range(sheet.cell_value(row, 2))
            current = {
                "id": item_id,
                "name": name,
                "range": range_text,
                "range_min": range_min,
                "range_max": range_max,
                "level": level,
                "parent_id": parent,
                "comment": comment,
                "workbook_row": row + 1,
                "repeating": not (range_min == 1 and range_max == 1),
                "fields": [],
            }
            groups.append(current)
            stack.append((level, item_id))
            continue
        if current is None:
            raise ExtractionError(f"row {row} data item {item_id} is outside a group")
        if level != current["level"] + 1:
            raise ExtractionError(
                f"row {row} data item {item_id} is L{level}; group {current['id']} is L{current['level']}"
            )
        raw_type = str(sheet.cell_value(row, 12))
        parsed = parse_type(raw_type)
        valid_set = _cell_str(sheet.cell_value(row, 13)) or None
        current["fields"].append(
            {
                "id": item_id,
                "name": name,
                "comment": comment,
                "optional": _option(mark, row),
                "valid_set": valid_set,
                "level": level,
                "order": len(current["fields"]),
                "workbook_row": row + 1,
                **parsed.to_json(),
            }
        )
    ids = [group["id"] for group in groups]
    if len(ids) != len(set(ids)):
        raise ExtractionError(f"version {version} has a repeated record type")
    for group in groups:
        field_ids = [field["id"] for field in group["fields"]]
        if len(field_ids) != len(set(field_ids)):
            raise ExtractionError(f"version {version} group {group['id']} repeats a data item")
    return {
        "version": version,
        "flow_name": flow_name,
        "sheet_row_start": start + 1,
        "sheet_row_end": end,
        "groups": groups,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tensions(versions: dict[str, dict]) -> list[dict]:
    found = []
    seen = set()
    for version, body in versions.items():
        for group in body["groups"]:
            for field in group["fields"]:
                if field["optional"]:
                    continue
                comment = field["comment"].lower()
                if not any(token in comment for token in ("null", "populated", "omit", "only")):
                    continue
                key = (group["id"], field["id"], field["comment"])
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "version_introduced_observation": version,
                        "record_type": group["id"],
                        "field_id": field["id"],
                        "name": field["name"],
                        "mark": "required",
                        "comment": field["comment"],
                        "resolution": (
                            "The level-column mark is required (1). The comment "
                            "describes a condition. Validation follows the mark."
                        ),
                    }
                )
    return found


def _field_map(version: dict, record_type: str) -> dict[str, dict]:
    for group in version["groups"]:
        if group["id"] == record_type:
            return {field["id"]: field for field in group["fields"]}
    return {}


def _group(version: dict, record_type: str) -> dict | None:
    for group in version["groups"]:
        if group["id"] == record_type:
            return group
    return None


def _disagreements(versions: dict[str, dict]) -> list[dict]:
    notes = []
    present = ", ".join(sorted(versions))
    notes.append(
        {
            "topic": "Which S0142 layout versions this workbook contains",
            "workbook": f"Sheet SO contains S0142 layouts {present}.",
            "other_source": (
                "s0142-workbook-evidence-2026-10-02.md concluded that the highest "
                "S0142 layout was v013 and that v014 was unpublished."
            ),
            "other_file": (
                "That note describes a different workbook, sha256 "
                "f0fc15bb3595de1c69615a2253c8c05ead35bae6d67bdf4f08a320399da03140, "
                "last saved by P415."
            ),
            "resolution": "This workbook is the layout authority. v014 is present.",
        }
    )
    added = []
    if "013" in versions and "014" in versions:
        for record_type in sorted(
            {group["id"] for group in versions["014"]["groups"]}
            & {group["id"] for group in versions["013"]["groups"]}
        ):
            old = _field_map(versions["013"], record_type)
            new = _field_map(versions["014"], record_type)
            for field_id in new:
                if field_id not in old:
                    field = new[field_id]
                    added.append(
                        {
                            "record_type": record_type,
                            "field_id": field_id,
                            "name": field["name"],
                            "data_type": field["data_type"],
                            "optional": field["optional"],
                            "comment": field["comment"],
                        }
                    )
    notes.append(
        {
            "topic": "Fields added in v014",
            "workbook": added,
            "other_source": (
                "The project notes describe five P444 fields N0674, N0675, N0676, "
                "N0677 and N0678. N0676 is named there only as a direct-compensation "
                "cashflow. The names above are the workbook item names."
            ),
            "resolution": "Field names, order, types and optionality come from the workbook.",
        }
    )
    unusual = []
    blanket = {"decimal(10,2)", "decimal(10,3)", "decimal(10,5)"}
    for group in versions[max(versions)]["groups"]:
        for field in group["fields"]:
            if field["kind"] in {"decimal", "number"} and field["data_type"] not in blanket:
                unusual.append(
                    {
                        "record_type": group["id"],
                        "field_id": field["id"],
                        "data_type": field["data_type"],
                        "name": field["name"],
                    }
                )
    notes.append(
        {
            "topic": "Precision is not one cashflow/price/volume pattern",
            "workbook": unusual,
            "other_source": (
                "The project notes summarise cashflows as decimal(10,2), prices as "
                "decimal(10,5) and volumes as decimal(10,3), with TLF/TLM (8,7), "
                "LoLP (10,9) and GBP-EUR (12,6) called out. The list above is every "
                "decimal or number type in the newest layout that is outside "
                "decimal(10,2), decimal(10,3) and decimal(10,5)."
            ),
            "resolution": "Each field keeps the type stored on its own workbook row.",
        }
    )
    mil = _group(versions[max(versions)], "MIL")
    notes.append(
        {
            "topic": "MIL record name",
            "workbook": None if mil is None else mil["name"],
            "other_source": (
                "The project notes say IDD Part 2 section 7.45 calls MIL "
                "'Minimum Export Limit'. Section 7.45 is the S0143 sub-flow, "
                "not S0142."
            ),
            "resolution": "The S0142 record name is the workbook group name.",
        }
    )
    terre = []
    for group in versions[max(versions)]["groups"]:
        if "terre" in group["comment"].lower() or "replacement reserve" in group["name"].lower():
            terre.append(
                {
                    "record_type": group["id"],
                    "name": group["name"],
                    "range": group["range"],
                    "range_min": group["range_min"],
                    "comment": group["comment"],
                }
            )
    notes.append(
        {
            "topic": "Replacement Reserve groups are not required on every run",
            "workbook": terre,
            "other_source": (
                "The project notes cite section 5.23 as saying Replacement Reserve "
                "items are sent on every run."
            ),
            "resolution": (
                "Workbook cardinality wins. A minimum of 0 means a valid file may "
                "omit the group. The validator does not require these records."
            ),
        }
    )
    apd = []
    for version, body in versions.items():
        group = _group(body, "APD")
        if group is not None:
            apd.append({"version": version, "range": group["range"]})
    notes.append(
        {
            "topic": "APD cardinality is not the same in every layout version",
            "workbook": apd,
            "other_source": "The project notes do not mention the v008 APD range.",
            "resolution": "Each layout version keeps the range on its own APD row.",
        }
    )
    return notes


def _unresolved(versions: dict[str, dict], sheet_names: list[str]) -> list[dict]:
    valid_sets = sorted(
        {
            field["valid_set"]
            for body in versions.values()
            for group in body["groups"]
            for field in group["fields"]
            if field["valid_set"]
        }
    )
    sign_hits = [
        f"{group['id']}.{field['id']}"
        for body in versions.values()
        for group in body["groups"]
        for field in group["fields"]
        if "sign" in f"{field['name']} {field['comment']}".lower()
    ]
    return [
        {
            "id": "sign-convention",
            "topic": "I014 sign convention",
            "detail": (
                "The S0142 rows do not state how to read the sign of a cashflow "
                "or a volume. "
                + (
                    "No S0142 name or comment contains 'sign'."
                    if not sign_hits
                    else "Cells mentioning sign: " + ", ".join(sorted(set(sign_hits)))
                )
                + " The validator stores the sign character and does not interpret it."
            ),
        },
        {
            "id": "decimal-length-parameter",
            "topic": "Meaning of the first number in decimal(n,m) and number(n,m)",
            "detail": (
                "The Items sheet headers are Length and Precision. For S0142 "
                "decimal types those two columns are empty; only the full type "
                "string is filled. Precision in the second parameter is enforced "
                "as a maximum number of digits after the decimal point. Whether "
                "the first parameter counts digits before the decimal point or "
                "total significant digits is not stated, so it is not a failing check."
            ),
        },
        {
            "id": "lexical-pictures",
            "topic": "Date, time and boolean spellings",
            "detail": (
                "The workbook type names are date, datetime and boolean, with no "
                "picture. Accepted forms are an assumption documented in the README: "
                "YYYYMMDD, YYYYMMDDHHMMSS, and a short boolean word list. "
                "Valid-set names referenced by S0142, whose members are not listed "
                f"in this workbook: {', '.join(valid_sets) if valid_sets else '(none)'}."
            ),
        },
        {
            "id": "physical-framing",
            "topic": "Delimiter, AAA header and ZZZ trailer",
            "detail": (
                "The S0142 blocks do not name a field separator or a file header "
                "or trailer. Pipes, an AAA file-type version and a ZZZ trailer are "
                "assumptions. AAA and ZZZ field lists are not validated. "
                f"Worksheets in this file: {', '.join(repr(name) for name in sheet_names)}."
            ),
        },
        {
            "id": "sibling-order",
            "topic": "Order of sibling groups",
            "detail": (
                "Parentage is taken from the L1-L9 marks. The workbook does not "
                "say that siblings must appear in sheet order. Sibling order is "
                "not enforced."
            ),
        },
        {
            "id": "which-version-applies",
            "topic": "Which layout a settlement date uses",
            "detail": (
                "v014 comments say the P444 fields are populated for post-P444 "
                "settlement dates. The workbook does not give that date, and it "
                "does not say which layout version a file of a given date must use. "
                "The parser reads the version from the file. It does not infer one "
                "from the settlement date."
            ),
        },
        {
            "id": "null-token",
            "topic": "How absence is written",
            "detail": (
                "Optional fields may be empty. The workbook does not say that the "
                "four characters NULL mean absent, and it does not say that an "
                "empty field means zero. Empty optional fields are loaded as SQL "
                "NULL. The token NULL is a bad value for a numeric type."
            ),
        },
        {
            "id": "same-as-null",
            "topic": "Null on N0388 and N0389",
            "detail": (
                "The workbook comment on N0388 says a null means the same value as "
                "Period BM Unit Total Accepted Bid Volume, and N0389 has a similar "
                "comment. That is not a deletion and not zero. This tool does not "
                "copy the other field into the empty one."
            ),
        },
        {
            "id": "omission-not-enforceable",
            "topic": "Cited omission rules that a single file cannot decide",
            "detail": (
                "Change-only omission, the II/RF full-set wording, the BO4 send "
                "condition, the incomplete BO7 sentence, and the missing D-run "
                "baseline are listed under omission_rules.not_enforced. They are "
                "not guessed."
            ),
        },
    ]


def _markdown(spec: dict) -> str:
    source = spec["source"]
    lines = [
        "# S0142 layout extracted from the IDD Part 2 workbook",
        "",
        "Independent tool; not affiliated with or endorsed by Elexon.",
        "",
        "Contains BSC information that is available from Elexon at no charge and which is licensed under the Elexon Public Data Licence.",
        "",
        "Generated by `s0142.extract` from the SO sheet. Field names and data-item ids are format facts from that sheet.",
        "",
        "## Source",
        "",
        f"- Official filename associated with this hash: `{source['official_filename']}`",
        f"- SHA-256: `{source['sha256']}`",
        f"- Size: {source['size_bytes']} bytes",
        f"- Sheet: `{source['sheet']}`",
        f"- Workbook last saved by: {source['last_saved_by']}",
        f"- Flow: {source['flow_id']} {source['flow_name']}",
        f"- Worksheets: {', '.join(repr(name) for name in source['sheet_names'])}",
        "",
        "## Versions",
        "",
        "| Version | Sheet rows | Record types | Data items |",
        "|---|---:|---:|---:|",
    ]
    for version, body in spec["versions"].items():
        fields = sum(len(group["fields"]) for group in body["groups"])
        lines.append(
            f"| {version} | {body['sheet_row_start']}-{body['sheet_row_end']} | "
            f"{len(body['groups'])} | {fields} |"
        )
    lines.extend(["", "## Record types", ""])
    for version, body in spec["versions"].items():
        lines.append(f"### v{version}")
        lines.append("")
        lines.append("| Record | Level | Parent | Range | Fields | Name |")
        lines.append("|---|---:|---|---|---:|---|")
        for group in body["groups"]:
            parent = group["parent_id"] or ""
            lines.append(
                f"| {group['id']} | {group['level']} | {parent} | {group['range']} | "
                f"{len(group['fields'])} | {group['name']} |"
            )
        lines.append("")
        for group in body["groups"]:
            lines.append(f"#### {group['id']} — {group['name']}")
            lines.append("")
            if group["comment"]:
                lines.append(f"Comment: {group['comment']}")
                lines.append("")
            lines.append("| # | Id | Name | Type | Optional | Valid set |")
            lines.append("|---:|---|---|---|---|---|")
            for field in group["fields"]:
                optional = "yes" if field["optional"] else "no"
                valid = field["valid_set"] or ""
                lines.append(
                    f"| {field['order'] + 1} | {field['id']} | {field['name']} | "
                    f"{field['data_type']} | {optional} | {valid} |"
                )
            lines.append("")
    lines.extend(["## Disagreements with the project notes", ""])
    for item in spec["disagreements"]:
        lines.append(f"### {item['topic']}")
        lines.append("")
        lines.append(f"Resolution: {item['resolution']}")
        lines.append("")
        lines.append("Workbook:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(item["workbook"], indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        lines.append(f"Other source: {item['other_source']}")
        lines.append("")
    lines.extend(["## Workbook tensions", ""])
    lines.append(
        "A required mark with a comment that talks about nulls or conditional population. The mark is what the validator enforces."
    )
    lines.append("")
    for item in spec["tensions"]:
        lines.append(
            f"- {item['record_type']}.{item['field_id']} ({item['name']}): {item['comment']}"
        )
    lines.extend(["", "## UNRESOLVED", ""])
    for item in spec["unresolved"]:
        lines.append(f"### {item['id']}")
        lines.append("")
        lines.append(item["detail"])
        lines.append("")
    lines.append("## Omission and deletion rules that are not workbook cells")
    lines.append("")
    lines.append(spec["omission_rules"]["source"])
    lines.append("")
    lines.append("Enforced deletion value fields:")
    lines.append("")
    for record_type, fields in spec["omission_rules"]["deletion_value_fields"].items():
        lines.append(f"- {record_type}: {', '.join(fields)}")
    lines.append("")
    lines.append("Not enforced:")
    lines.append("")
    for item in spec["omission_rules"]["not_enforced"]:
        lines.append(f"- `{item['id']}`: {item['reason']}")
    lines.append("")
    return "\n".join(lines)


def extract_workbook(path: Path) -> dict:
    path = Path(path)
    digest = _sha256(path)
    size = path.stat().st_size
    workbook = _open_workbook(path)
    if "SO" not in workbook.sheet_names():
        raise ExtractionError(f"workbook has no SO sheet: {workbook.sheet_names()}")
    sheet = workbook.sheet_by_name("SO")
    headers = [_cell_str(sheet.cell_value(0, column)) for column in range(sheet.ncols)]
    spans = _flow_spans(sheet)
    versions = {}
    flow_name = None
    for version, start, end in spans:
        parsed = _parse_version(sheet, version, start, end)
        flow_name = parsed["flow_name"]
        versions[version] = parsed
    spec = {
        "source": {
            "official_filename": OFFICIAL_FILENAME if digest == WORKBOOK_SHA256 else None,
            "sha256": digest,
            "expected_sha256": WORKBOOK_SHA256,
            "sha256_matches_expected": digest == WORKBOOK_SHA256,
            "size_bytes": size,
            "sheet": "SO",
            "sheet_names": workbook.sheet_names(),
            "columns": headers,
            "last_saved_by": workbook.user_name,
            "flow_id": _FLOW_ID,
            "flow_name": flow_name,
            "extractor": "s0142.extract",
        },
        "versions": versions,
        "disagreements": _disagreements(versions),
        "tensions": _tensions(versions),
        "unresolved": _unresolved(versions, workbook.sheet_names()),
        "omission_rules": rules_json(),
    }
    return spec


def write_spec(spec: dict, json_path: Path, markdown_path: Path) -> None:
    json_path.write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(spec), encoding="utf-8")
