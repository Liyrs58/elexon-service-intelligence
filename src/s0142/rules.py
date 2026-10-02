"""Omission and deletion rules cited for S0142.

These rules are not cells in the V43.0 workbook. They are taken from the
project notes on IDD Part 2 V55.0 section 5.23. Where a note and the workbook
disagree, the workbook cardinality and optionality win, and the disagreement
is recorded in the generated spec.

Only the checks in ``ENFORCED`` are applied to a file. Everything in
``NOT_ENFORCED`` stays UNRESOLVED: applying it would require a fact the
workbook and the cited section do not give us.
"""

from __future__ import annotations

SOURCE = (
    "IDD Part 2 V55.0 section 5.23, as cited by the project notes of "
    "2026-10-02. No cell in the V43.0 workbook states these rules."
)

# Run types for which the cited section says S0142 carries a full set.
FULL_RUNS = ("II", "RF")

# Record types sent on non-baseline runs only when a new data version exists.
CHANGE_ONLY_RECORD_TYPES = ("MEL", "MIL", "FP2", "BO2", "BO6")

# Baseline named by the cited section. Run type D has none.
CHANGE_ONLY_BASELINE = {
    "SF": "II",
    "R1": "II",
    "R2": "II",
    "R3": "II",
    "DF": "RF",
}

# Value fields that the cited deletion clause allows to be empty.
# The ids are the workbook data items whose names are the value-from / value-to
# fields on those record types. Time fields are not in this map.
DELETION_VALUE_FIELDS = {
    "FP2": ("N0111", "N0112"),
    "BO2": ("N0028", "N0029"),
    "BO6": ("N0026", "N0027"),
}

# A deleted acceptance keeps BO3, with the BO6 value fields empty.
DELETED_ACCEPTANCE = {
    "parent_record": "BO3",
    "child_record": "BO6",
    "value_fields": ("N0026", "N0027"),
}

NOT_ENFORCED = (
    {
        "id": "change-only-omission",
        "reason": (
            "MEL, MIL, FP2, BO2 and BO6 are cited as present on SF/R1/R2/R3 "
            "only when the data version changed relative to II, and on DF "
            "only when it changed relative to RF. The cited wording is that "
            "a change counts no matter what the value was. These records have "
            "no data-version field in the workbook, so a second file of values "
            "cannot show whether a record should have been omitted."
        ),
    },
    {
        "id": "full-set-ii-rf",
        "reason": (
            "The cited section says S0142 is a full set on II and RF. The "
            "workbook still gives MEL, MIL, FP2, BO2 and BO6 a minimum "
            "cardinality of 0. A missing optional record on II or RF is not "
            "reported as a defect."
        ),
    },
    {
        "id": "bo4-send-condition",
        "reason": (
            "BO4 is cited as sent on a price change, a BO2 change, or a "
            "non-zero or NZ-flagged acceptance volume. The workbook has no "
            "NZ-flag data item, and one file cannot show that a price changed."
        ),
    },
    {
        "id": "bo7-rule",
        "reason": (
            "The cited section 5.23 sentence for BO7 is incomplete and gives "
            "no rule. BO7 is checked only against its workbook type, "
            "optionality and parent."
        ),
    },
    {
        "id": "d-run-baseline",
        "reason": "No baseline run is stated for S0142 run type D.",
    },
    {
        "id": "replacement-reserve-every-run",
        "reason": (
            "The cited section says Replacement Reserve items are sent on "
            "every run. The workbook gives those groups a minimum cardinality "
            "of 0 and comments that several are reported for post-TERRE "
            "settlement dates only. The workbook wins: absence is not a defect."
        ),
    },
    {
        "id": "sign-convention",
        "reason": (
            "No I014 sign convention for cashflows or volumes is stated in "
            "the workbook or in the cited S0142 sections. Signs are stored "
            "as written and are not interpreted."
        ),
    },
)


def as_json() -> dict:
    return {
        "source": SOURCE,
        "full_runs": list(FULL_RUNS),
        "change_only_record_types": list(CHANGE_ONLY_RECORD_TYPES),
        "change_only_baseline": dict(CHANGE_ONLY_BASELINE),
        "deletion_value_fields": {k: list(v) for k, v in DELETION_VALUE_FIELDS.items()},
        "deleted_acceptance": {
            "parent_record": DELETED_ACCEPTANCE["parent_record"],
            "child_record": DELETED_ACCEPTANCE["child_record"],
            "value_fields": list(DELETED_ACCEPTANCE["value_fields"]),
        },
        "not_enforced": list(NOT_ENFORCED),
    }
