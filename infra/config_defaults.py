"""Configuration catalogue defaults (SRD 6.23, DR-10).

``CONFIG_DEFAULTS`` are the Config-table settings an administrator may change;
they are seeded once and never overwritten. ``SCENARIO_T0`` is written by the
reset action (WP-1), not seeded. ``SSM_PARAMETER_KEYS`` are deployment values
held in SSM Parameter Store under ``/aera/{env}/``.
"""

from decimal import Decimal

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
