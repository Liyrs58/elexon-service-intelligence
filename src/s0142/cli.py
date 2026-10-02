"""Command line for the S0142 parser and validator."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

from s0142 import __version__
from s0142.engine import load_file, render_text
from s0142.fixtures import DEFECT_KINDS, write_benchmark_file, write_fixtures
from s0142.specio import spec_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="s0142")
    parser.add_argument("--version", action="version", version=f"s0142 {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="parse a file and report structural defects")
    validate.add_argument("file", type=Path)
    validate.add_argument("--database", default=":memory:")
    validate.add_argument("--layout-version", default=None, help="layout to check against, instead of AAA")
    validate.add_argument("--json", action="store_true")

    parse = commands.add_parser("parse", help="load a file into a DuckDB database")
    parse.add_argument("file", type=Path)
    parse.add_argument("--database", required=True)
    parse.add_argument("--layout-version", default=None)

    extract = commands.add_parser("extract", help="extract the S0142 layout from the IDD workbook")
    extract.add_argument("--workbook", type=Path, required=True)
    extract.add_argument("--json-out", type=Path, default=None)
    extract.add_argument("--markdown-out", type=Path, default=None)

    fixtures = commands.add_parser("generate-fixtures", help="write synthetic fixtures")
    fixtures.add_argument("--directory", type=Path, default=Path("fixtures"))

    benchmark = commands.add_parser("benchmark", help="time parse and validate on a synthetic file")
    benchmark.add_argument("--records", type=int, default=1_000_000)
    benchmark.add_argument("--keep", type=Path, default=None, help="keep the synthetic file at this path")

    args = parser.parse_args(argv)
    if args.command == "validate":
        report, connection = load_file(args.file, database=args.database, version=args.layout_version)
        connection.close()
        if args.json:
            print(json.dumps(report.to_json(), indent=2))
        else:
            sys.stdout.write(render_text(report))
        return 0 if report.ok else 1
    if args.command == "parse":
        report, connection = load_file(args.file, database=str(args.database), version=args.layout_version)
        connection.close()
        sys.stdout.write(render_text(report))
        return 0 if report.ok else 1
    if args.command == "extract":
        from s0142.extract import extract_workbook, write_spec

        spec = extract_workbook(args.workbook)
        json_out = args.json_out or spec_path()
        markdown_out = args.markdown_out or spec_path().with_suffix(".md")
        write_spec(spec, json_out, markdown_out)
        print(f"wrote {json_out}")
        print(f"wrote {markdown_out}")
        print(f"sha256 {spec['source']['sha256']} match={spec['source']['sha256_matches_expected']}")
        return 0 if spec["source"]["sha256_matches_expected"] else 2
    if args.command == "generate-fixtures":
        write_fixtures(args.directory)
        print(f"wrote valid fixtures and {', '.join(DEFECT_KINDS)} under {args.directory}")
        return 0
    if args.command == "benchmark":
        return _benchmark(args.records, args.keep)
    parser.error(f"unknown command {args.command}")
    return 2


def _benchmark(records: int, keep: Path | None) -> int:
    if keep is None:
        handle = tempfile.NamedTemporaryFile(prefix="s0142-bench-", suffix=".txt", delete=False)
        path = Path(handle.name)
        handle.close()
    else:
        path = keep
    database = path.with_suffix(".duckdb")
    if database.exists():
        database.unlink()
    started = time.perf_counter()
    body = write_benchmark_file(path, body_records=records)
    generated = time.perf_counter()
    report, connection = load_file(path, database=str(database))
    connection.close()
    finished = time.perf_counter()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    payload = {
        "body_records": body,
        "requested_records": records,
        "generate_seconds": round(generated - started, 3),
        "parse_validate_seconds": round(finished - generated, 3),
        "max_rss_kb": rss_kb,
        "defects": len(report.defects),
        "layout_version": report.layout_version,
        "database": str(database),
        "file": str(path),
    }
    print(json.dumps(payload, indent=2))
    if keep is None:
        path.unlink(missing_ok=True)
        database.unlink(missing_ok=True)
    return 0 if report.ok else 1
