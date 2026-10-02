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
from s0142.rules import DELETION_VALUE_FIELDS
from s0142.specio import WORKBOOK_SHA256, load_spec, spec_path
from s0142.typesys import check_lexical, parse_type

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
        self.assertIn("decimal-length-parameter", unresolved)
        self.assertIn("omission-not-enforceable", unresolved)
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
        self.assertIsNone(check_lexical("1.234", parsed))
        self.assertIsNone(check_lexical("-12345678.123", parsed))
        self.assertEqual(check_lexical("1.2345", parsed), "bad_precision")
        self.assertEqual(check_lexical("NULL", parsed), "bad_type")
        self.assertIsNone(check_lexical("", parsed))

    def test_integer_length_is_enforced(self):
        parsed = parse_type("integer(2)")
        self.assertIsNone(check_lexical("46", parsed))
        self.assertEqual(check_lexical("100", parsed), "bad_type")
        self.assertEqual(check_lexical("1.0", parsed), "bad_type")


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
            self.assertEqual(report.defects, [], msg=render_valid(version, self.spec)[:200])
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
            seen = {item.code for item in report.defects}
            self.assertEqual(seen, codes, msg=kind)
            self.assertTrue(any(item.code == kind for item in report.defects))
        precision, _ = self._load("\n".join(defect_lines("bad_precision", self.spec)) + "\n")
        hit = next(item for item in precision.defects if item.code == "bad_precision")
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
        codes = {item.code for item in report.defects}
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


if __name__ == "__main__":
    unittest.main()
