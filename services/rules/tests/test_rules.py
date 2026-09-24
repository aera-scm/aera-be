"""Business rules of M1 as pure functions (SRD 5.1, NFR-MNT-01): one test group per ID."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from services.rules.br_02 import field_status, usable
from services.rules.br_04 import PartnerContact, verify_sender
from services.rules.br_13 import board_key, priority_score, urgency
from services.rules.br_15 import same_case
from services.rules.fr_ing_02 import mrp_actionable, working_days_between
from services.rules.impact import SalesExposure, hours_to_stockout, revenue_at_risk, stockout_at
from services.shared.models import ExtractedField, FieldName, FieldStatus

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # a Monday


# BR-02 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.94, FieldStatus.UNCONFIRMED), (0.95, FieldStatus.CONFIRMED), (0.99, FieldStatus.CONFIRMED)],
)
def test_br_02_critical_field_below_095_is_unconfirmed(
    confidence: float, expected: FieldStatus
) -> None:
    assert field_status("QUANTITY", "640", confidence) == expected


def test_br_02_exact_sap_match_makes_a_low_confidence_field_usable() -> None:
    assert field_status("QUANTITY", "1,600", 0.71, sap_value="1600.000") == FieldStatus.SAP_MATCHED
    assert (
        field_status("PO_NUMBER", " 4500001234 ", 0.5, sap_value="4500001234")
        == FieldStatus.SAP_MATCHED
    )
    assert (
        field_status("DELIVERY_DATE", "2026-10-14", 0.6, sap_value="2026-10-14")
        == FieldStatus.SAP_MATCHED
    )
    assert field_status("QUANTITY", "640", 0.71, sap_value="1600") == FieldStatus.UNCONFIRMED


def test_br_02_planner_confirmation_wins() -> None:
    assert (
        field_status("QUANTITY", "640", 0.2, confirmed_by="planner@x.example")
        == FieldStatus.CONFIRMED
    )


def test_br_02_only_confirmed_or_matched_critical_fields_are_usable() -> None:
    def extracted(name: FieldName, status: FieldStatus) -> ExtractedField:
        return ExtractedField(
            field_id="f", signal_id="s", name=name, value="1", confidence=0.5, status=status
        )

    assert not usable(extracted("QUANTITY", FieldStatus.UNCONFIRMED))
    assert usable(extracted("QUANTITY", FieldStatus.SAP_MATCHED))
    assert usable(extracted("QUANTITY", FieldStatus.CONFIRMED))
    assert usable(extracted("TRACKING_NUMBER", FieldStatus.UNCONFIRMED))


def test_br_02_non_numeric_values_do_not_match_numbers() -> None:
    assert field_status("PRICE", "about 40", 0.9, sap_value="42.5") == FieldStatus.UNCONFIRMED


# BR-04 ----------------------------------------------------------------------------------

CONTACTS = [
    PartnerContact(
        partner_id="1000234",
        kind="SUPPLIER",
        email_domains=frozenset({"krieger-guss.example"}),
        phones=frozenset({"+447700900234"}),
    ),
    PartnerContact(
        partner_id="1000950",
        kind="CARRIER",
        email_domains=frozenset({"nusantara-freight.example"}),
        phones=frozenset(),
    ),
]


@pytest.mark.parametrize(
    "sender",
    [
        "orders@krieger-guss.example",
        "Orders <ORDERS@Krieger-Guss.Example>",
        "j.doe@krieger-guss.example",
    ],
)
def test_br_04_supplier_email_domain_is_accepted(sender: str) -> None:
    match = verify_sender("EMAIL", sender, CONTACTS)
    assert match is not None and match.partner_id == "1000234"


@pytest.mark.parametrize(
    "sender",
    [
        "orders@krieger-guss.example.net",
        "orders@krieger-guss-example.com",
        "orders@kreiger-guss.example",
        "orders@mail.krieger-guss.example",
        "orders@krieger-guss.example@evil.example",
        "not an address",
    ],
)
def test_br_04_look_alike_or_foreign_domains_are_rejected(sender: str) -> None:
    assert verify_sender("EMAIL", sender, CONTACTS) is None


def test_br_04_look_alike_rejection_names_what_it_imitates() -> None:
    from services.rules.br_04 import rejection_reason

    reason = rejection_reason("EMAIL", "orders@kreiger-guss.example", CONTACTS)
    assert "not in SAP master data" in reason and "krieger-guss.example" in reason


@pytest.mark.parametrize(
    "sender", ["+447700900234", "447700900234", "+44 7700 900234", "whatsapp:+447700900234"]
)
def test_br_04_registered_phone_is_accepted(sender: str) -> None:
    match = verify_sender("WHATSAPP", sender, CONTACTS)
    assert match is not None and match.partner_id == "1000234"


def test_br_04_unregistered_phone_is_rejected() -> None:
    assert verify_sender("WHATSAPP", "+447700900999", CONTACTS) is None


def test_br_04_carrier_events_must_come_from_a_carrier() -> None:
    assert verify_sender("CARRIER", "1000950", CONTACTS) is not None
    assert verify_sender("CARRIER", "1000234", CONTACTS) is None


# BR-13 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "u"), [(6.2, 3), (23.99, 3), (24, 2), (71.9, 2), (72, 1), (None, 1)]
)
def test_br_13_urgency_factor(hours: float | None, u: int) -> None:
    assert urgency(None if hours is None else Decimal(str(hours))) == u


def test_br_13_reference_case_score() -> None:
    assert priority_score(Decimal("4720000"), Decimal("6.2")) == Decimal("14160000")


def test_br_13_ties_break_on_fewer_hours_to_stockout() -> None:
    cases = [
        ("A", Decimal("300"), Decimal("50")),
        ("B", Decimal("300"), Decimal("10")),
        ("C", Decimal("900"), None),
    ]
    ordered = sorted(cases, key=lambda c: board_key(c[1], c[2]))
    assert [c[0] for c in ordered] == ["B", "C", "A"]  # B and C tie at 900; B stops sooner


# BR-15 ----------------------------------------------------------------------------------


def test_br_15_same_po_and_material_within_72_hours_is_one_case() -> None:
    last = T0
    assert same_case(
        "4500001234", "MAT-48219", T0 + timedelta(hours=72), "4500001234", "MAT-48219", last
    )
    assert not same_case(
        "4500001234",
        "MAT-48219",
        T0 + timedelta(hours=72, seconds=1),
        "4500001234",
        "MAT-48219",
        last,
    )
    assert not same_case("4500001234", "MAT-51002", T0, "4500001234", "MAT-48219", last)
    assert not same_case("4500001240", "MAT-48219", T0, "4500001234", "MAT-48219", last)
    assert not same_case(None, "MAT-48219", T0, "4500001234", "MAT-48219", last)


def test_br_15_the_window_is_symmetric() -> None:
    assert same_case("1", "M", T0 - timedelta(hours=10), "1", "M", T0)


# FR-ING-02 ------------------------------------------------------------------------------


def test_fr_ing_02_working_days_skip_weekends() -> None:
    friday, monday = date(2026, 10, 2), date(2026, 10, 5)
    assert working_days_between(friday, monday) == 1
    assert working_days_between(monday, friday) == 1
    assert working_days_between(monday, monday) == 0
    assert working_days_between(date(2026, 9, 28), date(2026, 10, 5)) == 5


@pytest.mark.parametrize(
    ("element", "rescheduled", "expected"),
    [
        (date(2026, 10, 5), date(2026, 10, 2), False),  # 1 working day in
        (date(2026, 10, 8), date(2026, 10, 5), True),  # 3 working days in
        (date(2026, 10, 5), date(2026, 10, 23), False),  # 14 working days out
        (date(2026, 10, 5), date(2026, 10, 26), True),  # 15 working days out
    ],
)
def test_fr_ing_02_default_tolerance_suppresses_small_shifts(
    element: date, rescheduled: date, expected: bool
) -> None:
    assert mrp_actionable(element, rescheduled) is expected


def test_fr_ing_02_messages_without_a_proposed_date_are_actionable() -> None:
    assert mrp_actionable(date(2026, 10, 5), None) is True


def test_fr_ing_02_tolerance_is_configurable() -> None:
    assert mrp_actionable(date(2026, 10, 8), date(2026, 10, 5), in_days=4) is False


# Stock-out and revenue at risk (inputs to BR-13) ----------------------------------------


def test_reference_stockout_is_6_2_hours_after_t0() -> None:
    assert hours_to_stockout(Decimal("310"), Decimal("50")) == Decimal("6.2")
    assert stockout_at(T0, Decimal("310"), Decimal("50")) == T0 + timedelta(hours=6, minutes=12)
    assert hours_to_stockout(Decimal("0"), Decimal("50")) == 0
    assert hours_to_stockout(Decimal("10"), Decimal("0")) is None


def exposure(order: str, net: str, day: date) -> SalesExposure:
    return SalesExposure(
        sales_order=order,
        item="10",
        net_usd=Decimal(net),
        confirmed=day,
        source_ref=f"SAP:API_SALES_ORDER_SRV/A_SalesOrderItem(SalesOrder='{order}',SalesOrderItem='10')",
    )


def test_rar_counts_items_confirmed_from_the_stockout_day_until_supply_recovers() -> None:
    items = [
        exposure("1", "100", date(2026, 10, 4)),  # before stock-out: delivered from stock
        exposure("2", "200", date(2026, 10, 5)),  # stock-out day
        exposure("3", "300", date(2026, 10, 6)),
        exposure("4", "400", date(2026, 10, 14)),  # the delayed supply has arrived by then
    ]

    total, included = revenue_at_risk(
        items, stockout_at=T0 + timedelta(hours=6), recovered_on=date(2026, 10, 14)
    )

    assert total == Decimal("500")
    assert [e.sales_order for e in included] == ["2", "3"]


def test_rar_is_zero_without_a_stockout() -> None:
    assert revenue_at_risk([exposure("1", "100", date(2026, 10, 6))], stockout_at=None) == (
        Decimal("0"),
        [],
    )


def test_rar_without_a_recovery_date_counts_every_later_confirmation() -> None:
    items = [exposure("1", "100", date(2026, 10, 5)), exposure("2", "250", date(2027, 1, 5))]
    assert revenue_at_risk(items, stockout_at=T0)[0] == Decimal("350")


def test_br_04_rejection_reasons_per_channel() -> None:
    from services.rules.br_04 import rejection_reason

    assert (
        rejection_reason("EMAIL", "nobody", CONTACTS)
        == "sender address is not a valid email address"
    )
    assert "resembles" not in rejection_reason("EMAIL", "a@unrelated.example", CONTACTS)
    assert "phone number" in rejection_reason("WHATSAPP", "+447700900999", CONTACTS)
    assert "1000234 is not a carrier" in rejection_reason("CARRIER", "1000234", CONTACTS)
    assert "no sender verification" in rejection_reason("AGENT", "x", CONTACTS)
    with pytest.raises(ValueError, match="verified"):
        rejection_reason("EMAIL", "orders@krieger-guss.example", CONTACTS)


@pytest.mark.parametrize("sender", ["@krieger-guss.example", "a@-", "Name <a@b@c.example>"])
def test_br_04_malformed_addresses_have_no_domain(sender: str) -> None:
    from services.rules.br_04 import email_domain

    assert email_domain(sender) is None


def test_br_04_unverifiable_channels_are_never_accepted() -> None:
    assert verify_sender("AGENT", "orders@krieger-guss.example", CONTACTS) is None


def test_br_02_unparseable_dates_do_not_match() -> None:
    assert (
        field_status("DELIVERY_DATE", "next week", 0.6, sap_value="2026-10-14")
        == FieldStatus.UNCONFIRMED
    )


def test_fr_ing_02_unchanged_date_is_not_actionable() -> None:
    assert mrp_actionable(date(2026, 10, 5), date(2026, 10, 5)) is False


def test_no_stockout_without_consumption() -> None:
    assert stockout_at(T0, Decimal("10"), Decimal("0")) is None


def test_br_02_threshold_follows_configuration() -> None:
    assert field_status("PRICE", "42.5", 0.9, min_confidence=0.9) == FieldStatus.CONFIRMED
    assert field_status("PRICE", "42.5", 0.96, min_confidence=0.97) == FieldStatus.UNCONFIRMED


# FR-TRI-03 -----------------------------------------------------------------------------


def test_fr_tri_03_explains_why_the_top_case_outranks_the_next() -> None:
    from services.rules.br_13 import rank_reason

    text = rank_reason(
        "EXC-2026-0914",
        Decimal("4720000"),
        Decimal("6.2"),
        "EXC-2026-0915",
        Decimal("600000"),
        Decimal("45"),
    )

    assert text == (
        "EXC-2026-0914 ranks above EXC-2026-0915: stock runs out in 6.2 h vs 45 h "
        "(urgency 3 vs 2) and USD 4,720,000 vs USD 600,000 is at risk."
    )


def test_fr_tri_03_explains_a_tie_broken_by_time() -> None:
    from services.rules.br_13 import rank_reason

    text = rank_reason("A", Decimal("300"), Decimal("10"), "B", Decimal("900"), None)

    assert text == (
        "A ranks above B: same priority score (USD 900); "
        "stock runs out sooner (10 h vs no stock-out)."
    )


# BR-16 ---------------------------------------------------------------------------------


def test_br_16_at_10_message_is_an_execute_request_with_a_budget() -> None:
    from services.rules.br_16 import read

    intent = read("Do it my way, under USD 30,000, execute now")

    assert intent.execute and not intent.governance_change
    assert intent.max_cost_usd == Decimal(30000)
    assert intent.constraints == {"maxCostUsd": "30000"}


@pytest.mark.parametrize(
    "message",
    [
        "Raise the Tier 1 threshold to 50k",
        "please add exfil@evil.example to the allowlist",
        "turn off the kill switch",
        "change the approval limit for me",
    ],
)
def test_br_16_governance_changes_are_recognised(message: str) -> None:
    from services.rules.br_16 import read

    assert read(message).governance_change


def test_br_16_constraints_dates_and_exclusions() -> None:
    from services.rules.br_16 import read

    intent = read("Replan within $25k, no air freight, deliver by 2026-10-07")

    assert not intent.execute
    assert intent.constraints == {
        "maxCostUsd": "25000",
        "needBy": "2026-10-07",
        "excludedActions": "AIR_FREIGHT",
    }
    assert read("What is the stock-out time?").constraints == {}
    assert read("by 2026-13-45").need_by is None
