# S0142 parser and validator

Independent tool; not affiliated with or endorsed by Elexon.

Contains BSC information that is available from Elexon at no charge and which is licensed under the Elexon Public Data Licence.

This package reads Elexon SAA-I014 sub-flow 2 (S0142) settlement reports. It is a structural parser and validator. It does not calculate settlement, and it does not interpret the sign of a cashflow or a volume.

The **I014 sign convention is UNRESOLVED.** Nothing in the V43.0 workbook, nothing in the cited S0142 sections, and nothing in IDD Part 1 section 2.2 says whether a negative cashflow or volume is a payment, a receipt, an export, or an import. Values are stored with the sign that was written in the file.

## Run

Python 3.12. The runtime dependency is DuckDB.

```bash
pip install -e .
s0142 validate fixtures/valid_v014.txt
```

`s0142 parse FILE --database out.duckdb` loads the same file into DuckDB. `s0142 benchmark --records 1000000` times a synthetic file. Exit status is 0 when there are no hard defects. Style findings are warnings unless you pass `--strict`, which makes them fail the process.

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
- v014 adds five optional fields that v013 does not have: N0674 on APC (`decimal(10,2)`), N0677 on PPC (`decimal(10,2)`), N0678 on SSD (`decimal(10,2)`), N0675 and N0676 on BP7 (both `decimal(10,3)`). N0676 is Period Direct Compensation Cashflow.

## What was checked against IDD Part 1

File format rules come from NETA IDD Part 1 v64.0, effective 25 June 2026, sections 2.2.1, 2.2.2, 2.2.3, 2.2.11.24, 2.2.11.27 and 2.2.11.30. The HTML copy used for that reading has SHA-256 `2530cdcc980329d41466c043eeb1f0b776b3fdcf49f6310f1bf4bf057078d547`. It is not committed. The S0142 rows in the Part 2 workbook do not define this framing; Part 1 does.

Hard failures, because they catch corruption:

- Records are ASCII, separated by line feed. A CR, including CRLF, is `framing`. A missing final line feed is `framing`. A pipe follows the last field.
- AAA has 10 fields. File Type is `S0142` plus a three-digit version, for example `S0142014`. Message role is `D` or `R`. Creation time is `YYYYMMDDHHMMSS`. The test-data flag may be empty.
- ZZZ is required and must be last. Its record count includes the header and the footer (`record_count`). Its checksum is the 32-bit XOR described in section 2.2.2: each record except the trailer, line feed excluded, 4-byte big-endian chunks, short chunks padded with zero bytes (`checksum`).
- `decimal(n,d)` allows at most `d` digits after the point and at most `n-d` digits before it. `12345678.123` fails `decimal(10,3)`. The same limit is applied to `number(n,d)`.
- Dates are `YYYYMMDD`, times `HHMM`, timestamps `HHMMSS`. A leading `+` is not part of the numeric picture.

Warnings, until a real P114 portal file has been tested. `s0142 validate --strict` turns them into failures. Without `--strict` the exit status stays 0 when the only findings are warnings.

- Leading zeros and trailing zeros on decimals, for example `00.123` and `1.230`. The zero spellings `0`, `0.0`, `.0` and `0.` are valid, as are `-0`, `-0.0`, `-.0` and `-0.`. `0.123` and `.123` are valid. A decimal point with no digit is not.
- Leading zeros on integers.
- Booleans other than `T` or `F`. `TRUE`, `true`, `FALSE` and `t` warn. Any other token is `bad_type`.
- Leading or trailing spaces in text, and characters outside the section 2.2.3 set (for example `$`).
- Valid-set membership: Run Type `II`, `SF`, `R1`, `R2`, `R3`, `RF`, `D`, `DF`; Price Derivation Code `A`–`L`, `N`, `P`; P/C Flag `P` or `C`.

Header role codes are checked as `text(2)` only. The organisation list in section 2.2.1 is not applied. Quarter Hour Period has no member list in section 2.2.11, so N0602 is not membership-checked.

Also checked, from IDD Part 2 section 5.23 as cited in the project notes, and only where that citation names a record type:

- On FP2, BO2 and BO6, an empty value-from and value-to is the deletion encoding. Those fields are N0111/N0112, N0028/N0029 and N0026/N0027. Emptying any other field on that same record is a defect (`omission_deletion`).
- A BO6 record in that deletion shape whose open parent is not BO3 is a defect. The cited rule keeps BO3 when an acceptance is deleted.
- An empty optional field on any other record is not a deletion and is not zero. It is loaded as SQL NULL. The token `NULL` is a bad value for a numeric type.

The validator reports a line number and, where it applies, a field index and data-item id. Defect codes:

| Code | Meaning |
|---|---|
| `field_count` | The record's field count matches no layout, including the declared one. |
| `bad_type` | The value does not match that field's workbook type. |
| `bad_precision` | Too many digits after the point, or more than `n-d` digits before it. |
| `unknown_record` | The record type is not in the chosen layout. |
| `parent_child` | The open parent is not the parent given by the L columns. |
| `version_mismatch` | The AAA version and the record shapes do not agree, or the version cannot be chosen. |
| `omission_deletion` | A cited deletion rule is broken. |
| `missing_required` | A workbook-required field is empty. |
| `cardinality` | A group's count is outside its workbook range. |
| `framing` | AAA or ZZZ is missing or misplaced, the line ending is wrong, or the terminal pipe is missing. |
| `record_count` | The ZZZ count is not the number of records, header and footer included. |
| `checksum` | The ZZZ checksum is not the 32-bit XOR of the records before the trailer. |
| `trailing_zero`, `leading_zero`, `boolean_spelling`, `text_space`, `text_charset`, `valid_set` | Warnings unless `--strict`. |

## Where the notes and the workbook disagree

The workbook wins.

- `s0142-workbook-evidence-2026-10-02.md` said the highest S0142 layout was v013. That note describes a different file (SHA-256 `f0fc15bb3595de1c69615a2253c8c05ead35bae6d67bdf4f08a320399da03140`). This workbook contains v008 through v014.
- A blanket "cashflows are decimal(10,2), prices decimal(10,5), volumes decimal(10,3)" summary does not cover the sheet. Examples in v014 include `decimal(8,7)`, `decimal(8,5)`, `decimal(10,9)`, `decimal(12,6)`, `decimal(15,7)` and `number(4,1)`. Each field uses its own type string.
- Section 7.45, cited in the notes, calls MIL "Minimum Export Limit". On this sheet the S0142 group is "Maximum Import Limit Information". Section 7.45 is the S0143 sub-flow. S0143 rules are not applied here.
- Section 5.23, cited in the notes, says Replacement Reserve items are sent on every run. The workbook gives those groups a minimum of 0, and several comments limit them to post-TERRE settlement dates. Absence is not a defect.
- v008 gives APD the range `2`. From v009 the range is `1-2`. The notes do not mention that.

There is also a tension inside the workbook, not against the notes. BP7 field N0636 is marked required, and its comment says it is populated for post-ABSVD settlement dates only. The required mark is what fails a file. A real BP7 row that leaves N0636 empty, following the comment, is rejected.

## UNRESOLVED

These are not guessed. `s0142 validate` prints the ids on every run.

- **I014 sign convention** for cashflows and volumes. Stored as written. Not interpreted. Part 1 section 2.2 does not state it.
- **The baseline for change-only omissions.** MEL, MIL, FP2, BO2 and BO6 are cited as present on a non-baseline run only when the data version changed, and a change counts no matter what the value was. Those records have no data-version field, so one file cannot decide. The D-run baseline is not stated. The BO4 send condition cites an NZ flag that is not a data item here. The cited BO7 sentence is incomplete. II and RF are still allowed to omit those groups, because the workbook minimum is 0.
- **Whether a P114 portal download is byte-identical to these flows.** Part 1 describes the flow between central systems and parties. It does not say a portal extract is the same file. That is why the zero, boolean and text rules above are warnings until a real portal file has been tested.
- **Quarter Hour Period.** N0602 names that valid set. Section 2.2.11 has no matching heading, so membership is not checked.
- **Which layout a settlement date must use.** v014 comments mention post-P444 dates and do not give the date. The version is read from AAA, not from the settlement date.
- **N0388 and N0389.** A null means "same as" another volume. That is not a deletion and not zero. The empty field is left empty.

Sibling order is not a check. Parentage comes from the level columns. Part 1 describes nesting the same way and does not require sheet order. An empty optional field is absence. The characters `NULL` are not an empty field.

## Weakest assumptions, and what a real file could break

1. **A portal file may not be a flow file.** CRLF, `TRUE`, `1.230` and `00.123` are warnings in the default mode, so a portal extract that uses them still exits 0. `--strict` will reject that file. The byte-identity question is unresolved.
2. **The checksum excludes the trailer and the line ending.** A producer that XORs the ZZZ line, or that XORs the CR in a CRLF file, will fail `checksum`. The implementation follows the section 2.2.2 loop, including zero-padding a short 4-byte chunk. A hand vector is `AB` → `1094844416` (`0x41420000`).
3. **`TYPE|a|` with an empty last field and no extra terminal pipe is one field short, and it is also `framing`.** The pipe after the last field is required. A fixed-width or comma-separated file will not parse.
4. **N0636 is required whenever BP7 is present.** A production file that omits it outside the ABSVD window, as the comment suggests, is `missing_required`.
5. **Parentage is only the L-column tree.** MD2 is a child of SSD because MD2 is L4 and SSD is the preceding L3 group. A file that emits MD2 under a different L3 group is `parent_child`. Sibling order is free, so a file that relies on sheet order is accepted when the parent type happens to be open.
6. **Change-only files are not compared with a baseline.** An SF file that drops every unchanged MEL is accepted, because MEL's range is `0-*`. An SF file that repeats every MEL is also accepted.
7. **Deletion is only the empty value fields named above.** Removing BO3 instead of keeping BO3 and emptying BO6 is invisible. An empty MEL level is the optional mark, not a deletion. An empty field is not zero.
8. **No settlement arithmetic.** Period numbers are not required to be 1..50. Duplicate periods pass. Cashflows are not reconciled. The sign has no meaning. Header role codes are not matched to the organisation list, so `ZZ` passes as `text(2)`.
9. **AAA's test-data flag may be any `text(4)` or empty.** `OPER` is the operational spelling; other values are allowed for test phases and are not rejected.
10. **The version on the current portal is not named by this workbook.** A file may be v011, v014, or another version. This tool reads the version from the file.

Synthetic fixtures only. There is no real S0142 file in this repository. Party id `SYNTH001` and BM unit id `SYNTHBMU001` are made up.

## Benchmark

Parsing and validation of a synthetic v014 file with 1,000,000 body records (a valid skeleton plus MEL rows under the first BPI; MEL's range is `0-*`). The MEL levels are `1.2`, so the file has no trailing-zero warnings.

| Measure | Result |
|---|---|
| Body records | 1,000,000 |
| Defects | 0 |
| Warnings | 0 |
| Layout version | 014 |
| Generate the file | 1.872 s |
| Parse and validate | 13.979 s |
| Process max RSS | 176,756 KB |

Python 3.12.3, DuckDB 1.5.6. One run in this environment (`ru_maxrss` includes the generator). It is not a service level. The checksum of every record is included in that time.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Nineteen tests. One valid file per layout version, and one file per original defect class. Further tests cover the Part 1 rules: checksum vector `AB` → `1094844416`, record-count mismatch, missing ZZZ, CRLF, a missing terminal pipe, a short AAA, `12345678.123` on N0676, and the zero spellings `0`, `0.0`, `.0` and `0.`. The valid v014 file includes an FP2 row whose value fields are empty and whose time fields are present, which must pass. It also leaves N0316 empty, which must load as NULL and must not be reported as a deletion.
