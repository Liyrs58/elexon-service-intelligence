# S0142 parser and validator

Independent tool; not affiliated with or endorsed by Elexon.

Contains BSC information that is available from Elexon at no charge and which is licensed under the Elexon Public Data Licence.

This package reads Elexon SAA-I014 sub-flow 2 (S0142) settlement reports. It is a structural parser and validator. It does not calculate settlement, and it does not interpret the sign of a cashflow or a volume.

The **I014 sign convention is UNRESOLVED.** Nothing in the V43.0 workbook, and nothing in the cited S0142 sections, says whether a negative cashflow or volume is a payment, a receipt, an export, or an import. Values are stored with the sign that was written in the file.

## Run

Python 3.12. The runtime dependency is DuckDB.

```bash
pip install -e .
s0142 validate fixtures/valid_v014.txt
```

`s0142 parse FILE --database out.duckdb` loads the same file into DuckDB. `s0142 benchmark --records 1000000` times a synthetic file. Exit status is 0 when there are no defects.

Re-extract the layout from the official workbook (optional `xlrd` extra):

```bash
pip install -e '.[extract]'
s0142 extract --workbook NETA-IDD-Part-2_spreadsheet_v43.0.xls
```

## What was checked against the workbook

The layout authority is `NETA-IDD-Part-2_spreadsheet_v43.0.xls`, SHA-256 `814c506592f4418bf981693969c7402db45020fedfb8b47573d431eacb23a117`, 1,871,360 bytes. The committed spec was produced by `s0142.extract` walking the SO sheet. It was not typed in by hand. `src/s0142/data/s0142_layout.json` is the machine-readable spec. `src/s0142/data/s0142_layout.md` is the generated summary. Both record that hash.

Checked, for layouts v008, v009, v010, v011, v012, v013 and v014:

- Record-type ids, names, workbook order, L1–L9 level, and the parent implied by the previous group at the level above.
- Occurrence ranges (`1`, `2`, `1-*`, `0-*`, `46-50`, `0-1`, `0-2`, `1-2`).
- Data-item ids, names, order, type strings, the `1` / `O` mark, valid-set names, and comments.
- v014 adds five optional fields that v013 does not have: N0674 on APC (`decimal(10,2)`), N0677 on PPC (`decimal(10,2)`), N0678 on SSD (`decimal(10,2)`), N0675 and N0676 on BP7 (both `decimal(10,3)`). N0676 is Period Direct Compensation Cashflow. Its scale of 3 is enforced on that field alone.

Also checked, from IDD Part 2 section 5.23 as cited in the project notes, and only where that citation names a record type:

- On FP2, BO2 and BO6, an empty value-from and value-to is the deletion encoding. Those fields are N0111/N0112, N0028/N0029 and N0026/N0027. Emptying any other field on that same record is a defect (`omission_deletion`).
- A BO6 record in that deletion shape whose open parent is not BO3 is a defect. The cited rule keeps BO3 when an acceptance is deleted.
- An empty optional field on any other record is not a deletion and is not zero. It is loaded as SQL NULL. The token `NULL` is a bad value for a numeric type.

The validator reports a line number and, where it applies, a field index and data-item id. Defect codes:

| Code | Meaning |
|---|---|
| `field_count` | The record's field count matches no layout, including the declared one. |
| `bad_type` | The value does not match that field's workbook type. |
| `bad_precision` | Too many digits after the decimal point for that field's scale. |
| `unknown_record` | The record type is not in the chosen layout. |
| `parent_child` | The open parent is not the parent given by the L columns. |
| `version_mismatch` | The AAA version and the record shapes do not agree, or the version cannot be chosen. |
| `omission_deletion` | A cited deletion rule is broken. |
| `missing_required` | A workbook-required field is empty. |
| `cardinality` | A group's count is outside its workbook range. |
| `framing` | AAA or ZZZ is in the wrong place. |

## Where the notes and the workbook disagree

The workbook wins.

- `s0142-workbook-evidence-2026-10-02.md` said the highest S0142 layout was v013. That note describes a different file (SHA-256 `f0fc15bb3595de1c69615a2253c8c05ead35bae6d67bdf4f08a320399da03140`). This workbook contains v008 through v014.
- A blanket "cashflows are decimal(10,2), prices decimal(10,5), volumes decimal(10,3)" summary does not cover the sheet. Examples in v014 include `decimal(8,7)`, `decimal(8,5)`, `decimal(10,9)`, `decimal(12,6)`, `decimal(15,7)` and `number(4,1)`. Each field uses its own type string.
- Section 7.45, cited in the notes, calls MIL "Minimum Export Limit". On this sheet the S0142 group is "Maximum Import Limit Information". Section 7.45 is the S0143 sub-flow. S0143 rules are not applied here.
- Section 5.23, cited in the notes, says Replacement Reserve items are sent on every run. The workbook gives those groups a minimum of 0, and several comments limit them to post-TERRE settlement dates. Absence is not a defect.
- v008 gives APD the range `2`. From v009 the range is `1-2`. The notes do not mention that.

There is also a tension inside the workbook, not against the notes. BP7 field N0636 is marked required, and its comment says it is populated for post-ABSVD settlement dates only. The required mark is what fails a file. A real BP7 row that leaves N0636 empty, following the comment, is rejected.

## UNRESOLVED

These are not guessed.

- **I014 sign convention** for cashflows and volumes. Stored as written. Not interpreted.
- Whether the first number in `decimal(n,m)` and `number(n,m)` counts digits before the decimal point or total significant digits. The second number is enforced as the maximum number of fraction digits. The first number is not a failing check. `12345678.123` is accepted for `decimal(10,3)`.
- Lexical pictures. The workbook says `date`, `datetime` and `boolean` and does not give a picture. This tool accepts `YYYYMMDD`, `YYYYMMDDHHMMSS`, and the boolean spellings `T`, `F`, `TRUE`, `FALSE` in either case. That list is an assumption.
- Valid-set members. The sheet names Run Type, Price Derivation Code, P/C Flag and Quarter Hour Period. It does not list the members. Membership is not checked.
- The pipe separator, the `AAA` + `S0142` + three-digit version token, and the `ZZZ` trailer. None of these appear on the S0142 rows. AAA and ZZZ field lists are not validated. A missing ZZZ is not a defect.
- Sibling order. Parentage comes from the level columns. Siblings may appear in any order.
- Which layout a settlement date must use. v014 comments mention post-P444 dates and do not give the date. The version is read from the file, not from the settlement date.
- Change-only omission of MEL, MIL, FP2, BO2 and BO6. The cited rule turns on a new data version "no matter what the value was". Those records have no data-version field. A second file of values cannot decide it. The D-run baseline is not stated. The BO4 send condition cites an NZ flag that is not a data item on this sheet. The cited BO7 sentence is incomplete.
- "Full set" on II and RF. The workbook still allows zero MEL, MIL, FP2, BO2 and BO6 records. A missing optional group on II is not a defect.
- N0388 and N0389. The workbook comment says a null means "same as" another volume. That is not a deletion and not zero. The empty field is left empty.
- How absence is spelled, other than an empty field. The characters `NULL` are not defined as empty.

`s0142 validate` prints the unresolved ids on every run. The full wording is in the spec.

## Weakest assumptions, and what a real file could break

1. **The file is pipe-separated, one record per line, record type in the first field, with a terminal pipe.** The workbook does not say this. A fixed-width file, a comma-separated file, or a file without a terminal pipe whose last field is empty will be mis-counted. `TYPE|a|` (last field empty, no extra terminal pipe) is read as one field, not two.
2. **The layout version is the AAA file type `S0142` plus three digits, for example `S0142014`.** A real header that puts the version somewhere else, or omits AAA, falls through to a shape guess. v013 and v014 share most record shapes. A fragment that lacks APC, PPC, SSD and BP7 can match more than one version and is rejected as `version_mismatch` rather than parsed.
3. **Dates are `YYYYMMDD` and datetimes are `YYYYMMDDHHMMSS`, with no separators and no timezone.** A real file using `2026-01-01` or `2026/01/01` fails `bad_type`. GMT is not checked; the type has no zone.
4. **Booleans are the short word list above.** `0`, `1`, `Y` and `N` fail. The workbook comments use the word True and do not list the other spellings.
5. **`decimal(10,3)` does not limit digits before the decimal point.** A value that is illegal under one of the two common readings of `n` will still pass. The opposite is also true of text: length is counted in Python characters, not bytes, so a Windows-1252 file decoded as UTF-8 may fail before any field check.
6. **N0636 is required whenever BP7 is present.** A production file that omits it outside the ABSVD window, as the comment suggests, is a `missing_required` defect.
7. **Parentage is only the L-column tree.** MD2 is a child of SSD because MD2 is L4 and SSD is the preceding L3 group. If a real file emits MD2 under a different L3 group, that is `parent_child` even if a human reader would still call the file well formed. Sibling order is free, so the reverse mistake is also possible: a file that relies on sheet order, rather than nesting, is accepted when the parent record type happens to be open.
8. **Change-only files are not compared with a baseline.** An SF file that drops every unchanged MEL is accepted, because MEL's range is `0-*`. An SF file that repeats every MEL is also accepted. Neither result says the omission rule was followed.
9. **Deletion is only the empty value fields named above.** A deleted acceptance represented by removing BO3, rather than by keeping BO3 and emptying BO6, is invisible. An empty level on MEL is allowed by the `O` mark and is not treated as a deletion. Nothing in a general empty field means zero, and nothing in it means deleted.
10. **No cross-field or settlement checks.** Period numbers are not required to be 1..46, 1..48 or 1..50. Duplicate periods pass. Cashflows are not reconciled. The sign is not given a meaning. Run-type codes are not checked against a set, so `ZZ` is a legal `text(2)` if it fits the length.
11. **ZZZ is ignored apart from "it must be last".** A wrong record count or checksum in the trailer passes.
12. **The version in use on the P114 portal is not in this workbook.** The last public statement found by the project notes was version 11 in December 2021. A file that is a current portal extract may be v011, v014, or another version. This tool does not know which.

Synthetic fixtures only. There is no real S0142 file in this repository. Party id `SYNTH001` and BM unit id `SYNTHBMU001` are made up.

## Benchmark

Parsing and validation of a synthetic v014 file with 1,000,000 body records (a valid skeleton plus MEL rows under the first BPI; MEL's range is `0-*`):

| Measure | Result |
|---|---|
| Body records | 1,000,000 |
| Defects | 0 |
| Layout version | 014 |
| Generate the file | 0.118 s |
| Parse and validate | 9.152 s |
| Process max RSS | 200,328 KB |

Python 3.12.3, DuckDB 1.5.6. The figure is from one run in this environment (`ru_maxrss` is the process high-water mark, including the generator). It is not a service level.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

One valid file per layout version, and one file per defect class. The valid v014 file includes an FP2 row whose value fields are empty and whose time fields are present, which must pass. It also leaves N0316 empty, which must load as NULL and must not be reported as a deletion.
