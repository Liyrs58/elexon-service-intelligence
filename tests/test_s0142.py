"""Behaviour pinned to the extracted workbook spec and the synthetic fixtures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from s0142.engine import load_file
from s0142.fixtures import (
    DEFECT_KINDS,
    PARTY_ID,
    defect_lines,
    render_valid,
    valid_lines,
    write_fixtures,
)
from s0142.part1 import aaa_line, seal
from s0142.rules import DELETION_VALUE_FIELDS
from s0142.specio import WORKBOOK_SHA256, load_spec, spec_path
from s0142.part1 import checksum_records, xor_record
from s0142.typesys import check_lexical, parse_type


def _codes(findings) -> set[str]:
    return {item.code for item in findings}

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
WORKBOOK = Path("/home/ubuntu/.cursor/projects/workspace/uploads/p2v43_917f.xls")


class SpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_spec()

    def test_workbook_hash_is_recorded(self):
        source = self.spec.payload["source"]
        self.assertEqual(source["sha256"], WORKBOOK_SHA256)
        self.assertTrue(source["sha256_matches_expected"])
        self.assertEqual(source["official_filename"], "NETA-IDD-Part-2_spreadsheet_v43.0.xls")
        self.assertEqual(source["sheet"], "SO")
        self.assertIn("014", self.spec.versions)
        self.assertEqual(list(self.spec.versions), ["008", "009", "010", "011", "012", "013", "014"])

    def test_v014_p444_fields(self):
        layout = self.spec.versions["014"]
        previous = self.spec.versions["013"]
        expected = {
            ("APC", "N0674"): "decimal(10,2)",
            ("PPC", "N0677"): "decimal(10,2)",
            ("SSD", "N0678"): "decimal(10,2)",
            ("BP7", "N0675"): "decimal(10,3)",
            ("BP7", "N0676"): "decimal(10,3)",
        }
        for (record_type, field_id), data_type in expected.items():
            field = next(item for item in layout.by_id[record_type].fields if item.id == field_id)
            self.assertEqual(field.data_type, data_type)
            self.assertTrue(field.optional)
            self.assertNotIn(field_id, {item.id for item in previous.by_id[record_type].fields})
        n0676 = next(item for item in layout.by_id["BP7"].fields if item.id == "N0676")
        self.assertEqual(n0676.scale, 3)
        self.assertFalse(n0676.length_parameter_enforced)
        self.assertEqual(n0676.name, "Period Direct Compensation Cashflow")
        self.assertIn("\u00a0", n0676.raw_type)

    def test_hierarchy_and_apd_range(self):
        for version, layout in self.spec.versions.items():
            for group in layout.groups:
                for item in group.fields:
                    self.assertEqual(item.level, group.level + 1)
                if group.parent_id is not None:
                    self.assertIn(group.parent_id, layout.by_id)
                    self.assertEqual(layout.by_id[group.parent_id].level, group.level - 1)
            apd = layout.by_id["APD"]
            if version == "008":
                self.assertEqual((apd.range_min, apd.range_max), (2, 2))
            else:
                self.assertEqual((apd.range_min, apd.range_max), (1, 2))
        self.assertEqual(self.spec.versions["014"].by_id["MIL"].name, "Maximum Import Limit Information")
        self.assertEqual(self.spec.versions["014"].by_id["FP2"].parent_id, "BP7")
        self.assertEqual(self.spec.versions["014"].by_id["BO6"].parent_id, "BO3")

    def test_disagreements_and_unresolved_are_explicit(self):
        blob = json.dumps(self.spec.disagreements)
        self.assertIn("v013", blob)
        self.assertIn("MIL", blob)
        self.assertIn("Replacement Reserve", blob)
        self.assertIn("decimal(15,7)", blob)
        unresolved = {item["id"] for item in self.spec.unresolved}
        self.assertIn("sign-convention", unresolved)
        self.assertIn("omission-not-enforceable", unresolved)
        self.assertIn("portal-byte-identity", unresolved)
        self.assertIn("quarter-hour-period", unresolved)
        self.assertNotIn("physical-framing", unresolved)
        self.assertNotIn("decimal-length-parameter", unresolved)
        self.assertIn(
            "2530cdcc980329d41466c043eeb1f0b776b3fdcf49f6310f1bf4bf057078d547",
            json.dumps(self.spec.unresolved),
        )
        self.assertTrue(self.spec.payload["tensions"])
        self.assertEqual(self.spec.payload["tensions"][0]["field_id"], "N0636")
        markdown = spec_path().with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(WORKBOOK_SHA256, markdown)
        self.assertIn("Independent tool; not affiliated with or endorsed by Elexon.", markdown)

    def test_reextract_matches_committed_spec(self):
        if not WORKBOOK.is_file():
            self.skipTest("workbook is not in this environment")
        from s0142.extract import extract_workbook

        extracted = extract_workbook(WORKBOOK)
        self.assertEqual(extracted, self.spec.payload)

    def test_deletion_value_fields_exist_in_every_version(self):
        for layout in self.spec.versions.values():
            for record_type, field_ids in DELETION_VALUE_FIELDS.items():
                group = layout.by_id[record_type]
                present = {item.id for item in group.fields}
                for field_id in field_ids:
                    self.assertIn(field_id, present)
                    field = next(item for item in group.fields if item.id == field_id)
                    self.assertTrue(field.optional)


class LexicalTests(unittest.TestCase):
    def test_n0676_scale_is_field_specific(self):
        parsed = parse_type("decimal(10,3)")
        self.assertEqual(check_lexical("1.234", parsed), [])
        self.assertEqual(check_lexical("1234567.123", parsed), [])
        self.assertEqual(_codes(check_lexical("-12345678.123", parsed)), {"bad_precision"})
        self.assertEqual(_codes(check_lexical("1.2345", parsed)), {"bad_precision"})
        self.assertEqual(_codes(check_lexical("NULL", parsed)), {"bad_type"})
        self.assertEqual(check_lexical("", parsed), [])

    def test_integer_length_is_enforced(self):
        parsed = parse_type("integer(2)")
        self.assertEqual(check_lexical("46", parsed), [])
        self.assertEqual(_codes(check_lexical("100", parsed)), {"bad_type"})
        self.assertEqual(_codes(check_lexical("1.0", parsed)), {"bad_type"})
        self.assertEqual(_codes(check_lexical("01", parsed)), {"leading_zero"})
        self.assertEqual(check_lexical("0", parsed), [])
        self.assertEqual(check_lexical("-0", parsed), [])


class FileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_spec()

    def _load(self, text: str):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            path = Path(handle.name)
        report, connection = load_file(path)
        path.unlink()
        return report, connection

    def test_valid_fixture_for_every_version(self):
        for version in self.spec.versions:
            report, connection = self._load(render_valid(version, self.spec))
            self.assertEqual(report.errors, [], msg=render_valid(version, self.spec)[:400])
            self.assertEqual(report.warnings, [], msg=[item.message for item in report.warnings][:8])
            self.assertEqual(report.layout_version, version)
            self.assertEqual(report.declared_version, version)
            nulls = connection.execute("SELECT COUNT(*) FROM rec_spi WHERE n0316 IS NULL").fetchone()[0]
            zeros = connection.execute("SELECT COUNT(*) FROM rec_spi WHERE n0316 = '0'").fetchone()[0]
            party = connection.execute("SELECT n0045 FROM rec_srh").fetchone()[0]
            cashflow = connection.execute("SELECT n0048 FROM rec_apc").fetchone()[0]
            connection.close()
            self.assertEqual(nulls, 46)
            self.assertEqual(zeros, 0)
            self.assertEqual(party, PARTY_ID)
            self.assertTrue(str(cashflow).startswith("-"), msg=cashflow)
            self.assertEqual(len(report.deletion_signals), 1)
            self.assertEqual(report.deletion_signals[0].record_type, "FP2")

    def test_defect_classes(self):
        expected = {
            "field_count": {"field_count"},
            "bad_type": {"bad_type"},
            "bad_precision": {"bad_precision"},
            "unknown_record": {"unknown_record"},
            "parent_child": {"parent_child"},
            "version_mismatch": {"version_mismatch"},
            "omission_deletion": {"omission_deletion"},
        }
        for kind, codes in expected.items():
            report, connection = self._load("\n".join(defect_lines(kind, self.spec)) + "\n")
            connection.close()
            seen = {item.code for item in report.errors}
            self.assertEqual(seen, codes, msg=kind)
            self.assertTrue(any(item.code == kind for item in report.defects))
        precision, _ = self._load("\n".join(defect_lines("bad_precision", self.spec)) + "\n")
        hit = next(item for item in precision.errors if item.code == "bad_precision")
        self.assertEqual(hit.field_id, "N0676")
        self.assertIsNotNone(hit.line)

    def test_deleted_acceptance_without_bo3(self):
        lines = valid_lines("014", self.spec)
        inserted = False
        out = []
        for line in lines:
            out.append(line)
            if not inserted and line.startswith("BP7|"):
                out.append("BO6|0||30||")
                inserted = True
        self.assertTrue(inserted)
        report, connection = self._load("\n".join(out) + "\n")
        connection.close()
        codes = {item.code for item in report.errors}
        self.assertIn("omission_deletion", codes)
        self.assertIn("parent_child", codes)

    def test_committed_fixtures_match_the_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixtures(Path(directory), self.spec)
            for version in self.spec.versions:
                name = f"valid_v{version}.txt"
                self.assertEqual(
                    (FIXTURES / name).read_text(encoding="utf-8"),
                    (Path(directory) / name).read_text(encoding="utf-8"),
                )
            for kind in DEFECT_KINDS:
                name = f"defect_{kind}.txt"
                self.assertEqual(
                    (FIXTURES / name).read_text(encoding="utf-8"),
                    (Path(directory) / name).read_text(encoding="utf-8"),
                )

    def test_fixtures_are_synthetic(self):
        for path in FIXTURES.glob("*.txt"):
            text = path.read_text(encoding="utf-8")
            self.assertIn(PARTY_ID, text)
            self.assertLess(path.stat().st_size, 200_000)
            self.assertNotIn("NATIONALGRID", text.upper())


class Part1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_spec()

    def _load(self, text: str, strict: bool = False):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            path = Path(handle.name)
        report, connection = load_file(path, strict=strict)
        path.unlink()
        return report, connection

    def _load_bytes(self, payload: bytes, strict: bool = False):
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as handle:
            handle.write(payload)
            path = Path(handle.name)
        report, connection = load_file(path, strict=strict)
        path.unlink()
        return report, connection

    def test_checksum_vector_matches_the_pseudo_code(self):
        # "AB" is one big-endian chunk 0x41 0x42 0x00 0x00.
        self.assertEqual(xor_record(0, b"AB"), 0x41420000)
        self.assertEqual(checksum_records([b"AB", b"CD"]), 0x41420000 ^ 0x43440000)

        def reference(record: bytes) -> int:
            padded = record + bytes((4 - len(record) % 4) % 4)
            total = 0
            for offset in range(0, len(padded), 4):
                total ^= int.from_bytes(padded[offset : offset + 4], "big")
            return total

        self.assertEqual(xor_record(0, b"AB"), reference(b"AB"))
        self.assertEqual(checksum_records([b"ABCDE"]), reference(b"ABCDE"))

    def test_checksum_record_count_and_missing_trailer(self):
        text = render_valid("014", self.spec)
        lines = text.splitlines()
        count, digest = lines[-1].split("|")[1:3]
        wrong_sum = "0" if digest != "0" else "1"
        checksum_report, _ = self._load("\n".join(lines[:-1] + [f"ZZZ|{count}|{wrong_sum}|"]) + "\n")
        self.assertEqual({item.code for item in checksum_report.errors}, {"checksum"})

        wrong_count = str(int(count) + 1)
        count_report, _ = self._load("\n".join(lines[:-1] + [f"ZZZ|{wrong_count}|{digest}|"]) + "\n")
        self.assertEqual({item.code for item in count_report.errors}, {"record_count"})

        missing, _ = self._load("\n".join(lines[:-1]) + "\n")
        self.assertIn("ZZZ trailer is missing.", [item.message for item in missing.errors])
        self.assertEqual({item.code for item in missing.errors}, {"framing"})

    def test_crlf_terminal_pipe_and_aaa_width(self):
        text = render_valid("014", self.spec)
        crlf, _ = self._load_bytes(text.replace("\n", "\r\n").encode("ascii"))
        self.assertEqual({item.code for item in crlf.errors}, {"framing"})
        self.assertTrue(any("CR" in item.message for item in crlf.errors))
        self.assertFalse(any(item.code in {"checksum", "record_count"} for item in crlf.errors))

        body = [line for line in text.splitlines() if not line.startswith("ZZZ|")]
        for index, line in enumerate(body):
            if line.startswith("SRH|"):
                body[index] = line[:-1]
                break
        pipe, _ = self._load("\n".join(seal(body)) + "\n")
        self.assertEqual({item.code for item in pipe.errors}, {"framing"})
        self.assertTrue(any("pipe after the last field" in item.message for item in pipe.errors))

        body[0] = "AAA|S0142014|"
        # restore the SRH pipe before sealing the short header, using a fresh body
        fresh = [line for line in text.splitlines() if not line.startswith("ZZZ|")]
        fresh[0] = "AAA|S0142014|"
        short, _ = self._load("\n".join(seal(fresh)) + "\n")
        self.assertEqual({item.code for item in short.errors}, {"framing"})
        self.assertTrue(any("10" in item.message for item in short.errors))

    def test_decimal_integer_digits_fail_and_listed_zeros_do_not_warn(self):
        parsed = parse_type("decimal(10,3)")
        self.assertTrue(any("before the decimal point" in item.detail for item in check_lexical("12345678.123", parsed)))
        for spelling in ("0", "0.0", ".0", "0.", "-0", "-0.0", "-.0", "-0."):
            self.assertEqual(check_lexical(spelling, parsed), [], msg=spelling)
        self.assertEqual(_codes(check_lexical("0.1230", parse_type("decimal(10,4)"))), {"trailing_zero"})
        self.assertEqual(_codes(check_lexical("1.230", parsed)), {"trailing_zero"})
        self.assertEqual(_codes(check_lexical("00.123", parsed)), {"leading_zero"})
        self.assertEqual(check_lexical("0.123", parsed), [])
        self.assertEqual(check_lexical(".123", parsed), [])
        self.assertEqual(_codes(check_lexical("0.1230", parsed)), {"bad_precision"})

        for spelling in ("0", "0.0", ".0", "0."):
            report, _ = self._load(self._with_n0676(spelling))
            self.assertEqual(report.errors, [], msg=spelling)
            self.assertEqual(report.warnings, [], msg=spelling)

        over, _ = self._load(self._with_n0676("12345678.123"))
        self.assertEqual({item.code for item in over.errors}, {"bad_precision"})
        self.assertEqual(over.errors[0].field_id, "N0676")
        self.assertIn("before the decimal point", over.errors[0].message)

    def test_style_warnings_and_strict_mode(self):
        cases = {
            "1.230": "trailing_zero",
            "00.123": "leading_zero",
        }
        for spelling, code in cases.items():
            report, _ = self._load(self._with_n0676(spelling))
            self.assertTrue(report.ok)
            self.assertEqual({item.code for item in report.warnings}, {code})
            strict, _ = self._load(self._with_n0676(spelling), strict=True)
            self.assertFalse(strict.ok)
            self.assertEqual({item.code for item in strict.warnings}, {code})

        boolean, _ = self._load(self._replace_field("SPI", "N0317", "TRUE"))
        self.assertTrue(boolean.ok)
        self.assertEqual({item.code for item in boolean.warnings}, {"boolean_spelling"})
        self.assertEqual(_codes(check_lexical("true", parse_type("boolean"))), {"boolean_spelling"})
        self.assertEqual(_codes(check_lexical("FALSE", parse_type("boolean"))), {"boolean_spelling"})
        self.assertEqual(_codes(check_lexical("t", parse_type("boolean"))), {"boolean_spelling"})
        self.assertEqual(check_lexical("T", parse_type("boolean")), [])
        self.assertEqual(_codes(check_lexical("YES", parse_type("boolean"))), {"bad_type"})

        spaced, _ = self._load(self._replace_field("SRH", "N0045", " SYNTH01"))
        self.assertEqual({item.code for item in spaced.warnings}, {"text_space"})
        foreign, _ = self._load(self._replace_field("SRH", "N0045", "SYNTH00$"))
        self.assertEqual({item.code for item in foreign.warnings}, {"text_charset"})

        run_type, _ = self._load(self._replace_field("SRH", "N0202", "ZZ"))
        self.assertEqual({item.code for item in run_type.warnings}, {"valid_set"})
        price, _ = self._load(self._replace_field("SPI", "N0439", "M"))
        self.assertEqual({item.code for item in price.warnings}, {"valid_set"})
        flag, _ = self._load(self._replace_field("APD", "N0177", "X"))
        self.assertEqual({item.code for item in flag.warnings}, {"valid_set"})
        self.assertEqual(check_lexical("II", parse_type("text(2)")), [])
        from s0142.part1 import VALID_SETS

        self.assertNotIn("Quarter Hour Period", VALID_SETS)

    def test_header_details(self):
        lines = [line for line in valid_lines("014", self.spec) if not line.startswith("ZZZ|")]
        lines[0] = aaa_line("014", test_flag="")
        omitted, _ = self._load("\n".join(seal(lines)) + "\n")
        self.assertEqual(omitted.errors, [])
        self.assertEqual(omitted.warnings, [])

        lines[0] = aaa_line("014").replace("|SA|", "|ZZ|", 1)
        role, _ = self._load("\n".join(seal(lines)) + "\n")
        self.assertEqual(role.errors, [])
        self.assertFalse(any(item.code == "valid_set" for item in role.warnings))

        bare = render_valid("014", self.spec).encode("ascii")
        self.assertTrue(bare.endswith(b"\n"))
        missing_lf, _ = self._load_bytes(bare[:-1])
        self.assertTrue(any("line feed" in item.message for item in missing_lf.errors))

    def _with_n0676(self, spelling: str) -> str:
        return self._replace_field("BP7", "N0676", spelling)

    def _replace_field(self, record_type: str, field_id: str, spelling: str) -> str:
        group = self.spec.versions["014"].by_id[record_type]
        index = next(item.order for item in group.fields if item.id == field_id)
        lines = [line for line in valid_lines("014", self.spec) if not line.startswith("ZZZ|")]
        done = False
        for position, line in enumerate(lines):
            if line.startswith(record_type + "|"):
                parts = line.split("|")
                parts[index + 1] = spelling
                lines[position] = "|".join(parts)
                done = True
                break
        if not done:
            self.fail(f"{record_type} is not in the valid v014 fixture")
        return "\n".join(seal(lines)) + "\n"


if __name__ == "__main__":
    unittest.main()
