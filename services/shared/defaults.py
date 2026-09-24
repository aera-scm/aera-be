"""Configuration catalogue defaults (SRD 6.23, DR-10).

``CONFIG_DEFAULTS`` are the Config-table settings an administrator may change;
they are seeded once and never overwritten. ``SCENARIO_T0`` is written by the
reset action (WP-1), not seeded. ``SSM_PARAMETER_KEYS`` are deployment values
held in SSM Parameter Store under ``/aera/{env}/``.
"""

from decimal import Decimal
from typing import NotRequired, TypedDict

CONFIG_DEFAULTS: dict[str, Decimal | str] = {
    "TIER1_MAX_USD": Decimal("25000"),
    "TIER1_MIN_CONFIDENCE": Decimal("0.85"),
    "CRITICAL_FIELD_MIN_CONF": Decimal("0.95"),
    "GROUNDING_MIN": Decimal("0.7"),
    "AUDIT_SAMPLE_RATE": Decimal("0.2"),
    "MAX_ITERATIONS": Decimal("20"),
    "MAX_TOKENS": Decimal("150000"),
    "MAX_RUN_SECONDS": Decimal("300"),
    "DONOR_MIN_COVER_DAYS": Decimal("2.0"),
    # Capped by BR-14 at runtime; the cap is not configuration.
    "REOPEN_GRACE_HOURS": Decimal("2"),
    "APPROVAL_MAX_HOURS": Decimal("4"),
    # Fraction of the remaining approval time at which the reminder is sent.
    "APPROVAL_REMINDER_AT": Decimal("0.5"),
    "SUPPLIER_REPLY_TIMEOUT_HOURS": Decimal("4"),
    "MRP_TOLERANCE_IN_DAYS": Decimal("3"),
    "MRP_TOLERANCE_OUT_DAYS": Decimal("15"),
    "KILL_SWITCH": "off",
}

SSM_PARAMETER_KEYS: tuple[str, ...] = (
    "MODEL_SUPERVISOR_ID",
    "MODEL_SMALL_ID",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
    "AR_POLICY_ARN",
    "SAP_READ_BASE",
    "SAP_WRITE_BASE",
    "SAP_SANDBOX_BASE",
)


class RateCardEntry(TypedDict):
    entryId: str
    actionType: str
    fromPlant: NotRequired[str]
    toPlant: NotRequired[str]
    supplierId: NotRequired[str]
    lane: NotRequired[str]
    unitCostUsd: Decimal
    fixedCostUsd: Decimal
    leadTimeHours: Decimal
    validFrom: str
    validTo: str


class ApproverLimit(TypedDict):
    userId: str
    role: str
    plant: str
    limitUsd: Decimal
    validFrom: str
    validTo: str


# DR-11 rate card for the reference scenario (SRD 6.6.3). Synthetic; the alternate
# supplier's lead time is not given by the SRD and is a seed assumption (36 h).
RATE_CARD: tuple[RateCardEntry, ...] = (
    {
        "entryId": "RC-STO-1020-1010",
        "actionType": "STO",
        "fromPlant": "1020",
        "toPlant": "1010",
        "unitCostUsd": Decimal("0"),
        "fixedCostUsd": Decimal("4100"),
        "leadTimeHours": Decimal("5"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
    },
    {
        "entryId": "RC-AIR-1000234",
        "actionType": "AIR_FREIGHT",
        "supplierId": "1000234",
        "lane": "DE-ID",
        "unitCostUsd": Decimal("0"),
        "fixedCostUsd": Decimal("38200"),
        "leadTimeHours": Decimal("17"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
    },
    {
        "entryId": "RC-ALT-1000871",
        "actionType": "ALTERNATE_SUPPLIER",
        "supplierId": "1000871",
        "unitCostUsd": Decimal("64.875"),
        "fixedCostUsd": Decimal("0"),
        "leadTimeHours": Decimal("36"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
    },
)

# DR-12 approver limits (OI-06): synthetic users until the owner names real ones. The
# backup approver covers the USD 42,300 reference plan (BR-23).
APPROVER_LIMITS: tuple[ApproverLimit, ...] = (
    {
        "userId": "approver@meridian-motors.example",
        "role": "approver",
        "plant": "1010",
        "limitUsd": Decimal("50000"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
    },
    {
        "userId": "backup.approver@meridian-motors.example",
        "role": "approver",
        "plant": "1010",
        "limitUsd": Decimal("100000"),
        "validFrom": "2026-01-01",
        "validTo": "9999-12-31",
    },
)
