"""Taking a facility out early, what it costs, and what it saves."""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from capstack.debt import CapitalStructure, DebtSchedule, Tranche, TrancheKind
from capstack.events import (
    AddOn,
    Draw,
    Recapitalisation,
    Refinancing,
    RefinancingError,
)
from capstack.money import ZERO, is_close, money
from capstack.periods import Frequency, PeriodGrid
from capstack.report import prepare
from capstack.spec import DealSpecError, load_deal, parse_deal

THORNBURY = str(Path(__file__).resolve().parents[1] / "examples" / "thornbury.json")
KESTREL = str(Path(__file__).resolve().parents[1] / "examples" / "kestrel.json")

CLOSE = date(2026, 6, 30)


def structure(minimum_cash: int = 20) -> CapitalStructure:
    return CapitalStructure.of(
        [
            Tranche.of(
                "Revolver",
                TrancheKind.REVOLVER,
                0,
                commitment=50,
                cash_rate="0.03",
                floating=False,
            ),
            Tranche.of("Old TLB", TrancheKind.TERM_LOAN, 500, cash_rate="0.08", floating=False),
            Tranche.of("New TLB", TrancheKind.TERM_LOAN, 0, cash_rate="0.05", floating=False),
        ],
        minimum_cash=minimum_cash,
    )


def run(
    events: list[Refinancing] | None = None,
    *,
    flows: int = 90,
    opening_cash: int = 60,
    minimum_cash: int = 20,
    **extra: Any,
) -> DebtSchedule:
    periods = list(PeriodGrid.build(CLOSE, years=5, frequency=Frequency.ANNUAL))
    return DebtSchedule.run(
        structure(minimum_cash),
        periods,
        [money(flows)] * len(periods),
        opening_cash=opening_cash,
        refinancings=events or [],
        **extra,
    )


def takeout(**fields: Any) -> Refinancing:
    kwargs: dict[str, Any] = {
        "into": [Draw.of("New TLB", 400)],
        "call_premium_rate": "0.02",
    }
    kwargs.update(fields)
    period = kwargs.pop("period", 2)
    tranche = kwargs.pop("tranche", "Old TLB")
    return Refinancing.of(period, tranche, **kwargs)


class TestTheEvent:
    def test_the_uses_are_the_balance_and_the_premium_on_it(self) -> None:
        event = takeout()
        assert event.premium_on(money(400)) == money(8)
        assert event.uses(money(400)) == money(408)

    def test_the_new_paper_nets_its_fees_and_discount(self) -> None:
        event = takeout(
            into=[Draw.of("New TLB", 400, issue_price="0.99", financing_fee_rate="0.01")]
        )
        assert event.face == money(400)
        assert event.discount == money(4)
        assert event.financing_fees == money(4)
        assert event.proceeds == money(392)

    def test_cash_is_the_plug_for_whatever_the_new_paper_misses(self) -> None:
        event = takeout(into=[Draw.of("New TLB", 380)])
        assert event.from_cash(money(400)) == money(28)

    def test_raising_more_than_the_balance_leaves_the_surplus_as_cash(self) -> None:
        event = takeout(into=[Draw.of("New TLB", 450)])
        assert event.from_cash(money(400)) == money(-42)

    def test_a_takeout_with_no_facility_named(self) -> None:
        with pytest.raises(RefinancingError, match="name the facility"):
            Refinancing.of(2, "  ")

    def test_a_premium_of_everything(self) -> None:
        with pytest.raises(RefinancingError, match="not a premium"):
            Refinancing.of(2, "Old TLB", call_premium_rate=1)

    def test_a_negative_write_off_is_a_credit(self) -> None:
        with pytest.raises(RefinancingError, match="a credit, not a cost"):
            Refinancing.of(2, "Old TLB", unamortised_fees=-1)

    def test_periods_are_numbered_from_one(self) -> None:
        with pytest.raises(RefinancingError, match="numbered from one"):
            Refinancing.of(0, "Old TLB")

    def test_the_same_tranche_drawn_twice(self) -> None:
        with pytest.raises(RefinancingError, match="drawn once"):
            Refinancing.of(
                2, "Old TLB", [Draw.of("New TLB", 100), Draw.of("New TLB", 50)]
            )

    def test_a_takeout_needs_a_label(self) -> None:
        with pytest.raises(RefinancingError, match="needs a label"):
            Refinancing.of(2, "Old TLB", label=" ")


class TestTheSchedule:
    def test_a_schedule_with_no_takeouts_is_unchanged(self) -> None:
        assert run().periods == run([]).periods

    def test_the_old_balance_goes_to_zero_and_the_new_paper_lands(self) -> None:
        schedule = run([takeout()])
        row = schedule[1]
        assert row.tranche("Old TLB").closing == ZERO
        assert row.tranche("New TLB").closing == money(400)
        assert row.tranche("New TLB").refinancing_draw == money(400)
        assert row.tranche("Old TLB").refinancing_repayment > 0

    def test_the_following_period_opens_on_the_new_paper(self) -> None:
        schedule = run([takeout()])
        assert schedule[2].tranche("Old TLB").opening == ZERO
        assert schedule[2].tranche("New TLB").opening == money(400)

    def test_the_cheaper_coupon_shows_up_in_the_interest(self) -> None:
        after = run([takeout()])[2].cash_interest
        before = run()[2].cash_interest
        assert after < before

    def test_a_matured_facility_does_not_keep_amortising_from_zero(self) -> None:
        """The old tranche sits at zero; a repayment against it would be invented."""
        schedule = run([takeout()])
        for row in schedule.periods[2:]:
            assert row.tranche("Old TLB").mandatory_repayment == ZERO
            assert row.tranche("Old TLB").cash_interest == ZERO

    def test_every_row_still_reconciles(self) -> None:
        schedule = run([takeout()])
        for row in schedule:
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()

    def test_repaying_into_the_same_facility_nets_to_the_difference(self) -> None:
        """A repricing, not two balances carried at once."""
        schedule = run([takeout(into=[Draw.of("Old TLB", 380)])])
        row = schedule[1]
        assert row.tranche("Old TLB").closing == money(380)
        assert row.tranche("Old TLB").refinancing_draw == money(380)
        assert row.tranche("Old TLB").refinancing_repayment > money(380)
        assert row.reconciles()

    def test_cash_pays_for_the_part_the_new_paper_does_not_raise(self) -> None:
        with_takeout = run([takeout(into=[Draw.of("New TLB", 380)])])
        without = run()
        event = with_takeout.refinancings[0]
        assert event.from_cash > 0
        assert is_close(
            without[1].closing_cash - with_takeout[1].closing_cash, event.from_cash
        )

    def test_the_totals_add_across_the_hold(self) -> None:
        schedule = run([takeout(unamortised_fees=7)])
        event = schedule.refinancings[0]
        assert schedule.total_refinanced == event.repaid
        assert schedule.total_call_premiums == event.call_premium
        assert schedule.total_fees_written_off == money(7)

    def test_the_write_off_moves_no_balance_and_no_cash(self) -> None:
        """It is a charge against earnings; nothing in this model responds."""
        plain = run([takeout()])
        charged = run([takeout(unamortised_fees=25)])
        assert [p.closing_cash for p in plain] == [p.closing_cash for p in charged]
        assert [p.closing_debt for p in plain] == [p.closing_debt for p in charged]
        assert charged.total_fees_written_off == money(25)

    def test_a_facility_with_nothing_left_cannot_be_refinanced(self) -> None:
        with pytest.raises(RefinancingError, match="nothing outstanding"):
            run([takeout(period=2, tranche="New TLB")])

    def test_a_takeout_the_business_cannot_fund(self) -> None:
        with pytest.raises(RefinancingError, match="which leaves only"):
            run([takeout(into=[])])

    def test_a_takeout_beyond_the_schedule(self) -> None:
        with pytest.raises(RefinancingError, match="beyond the 5 periods"):
            run([takeout(period=9)])

    def test_two_takeouts_in_one_period(self) -> None:
        with pytest.raises(RefinancingError, match="combine them into one"):
            run([takeout(label="One"), takeout(label="Two")])

    def test_an_unknown_facility(self) -> None:
        with pytest.raises(RefinancingError, match="no tranche named 'Mezzanine'"):
            run([takeout(tranche="Mezzanine")])

    def test_an_unknown_tranche_in_the_new_paper(self) -> None:
        with pytest.raises(RefinancingError, match="no tranche named 'Notes'"):
            run([takeout(into=[Draw.of("Notes", 400)])])

    def test_refinancing_a_revolver_says_nothing(self) -> None:
        with pytest.raises(RefinancingError, match="is a revolver"):
            run([takeout(tranche="Revolver")])

    def test_a_takeout_landing_on_a_recapitalisation(self) -> None:
        with pytest.raises(RefinancingError, match="cannot say which came first"):
            run(
                [takeout(period=2)],
                recapitalisations=[
                    Recapitalisation.of(2, [Draw.of("Old TLB", 50)], from_cash=1)
                ],
            )

    def test_a_takeout_landing_on_an_acquisition(self) -> None:
        with pytest.raises(RefinancingError, match="cannot say which came first"):
            run(
                [takeout(period=2)],
                acquisitions=[AddOn.of(2, ebitda=1, multiple=2)],
            )


class TestWhatItSaved:
    def test_the_spread_is_the_old_coupon_less_the_new_one(self) -> None:
        event = run([takeout()]).refinancings[0]
        assert event.old_rate == money("0.08")
        assert event.new_rate == money("0.05")
        assert event.spread_saved == money("0.03")

    def test_the_new_rate_is_weighted_across_the_paper_that_replaced_it(self) -> None:
        event = run(
            [takeout(into=[Draw.of("New TLB", 200), Draw.of("Old TLB", 200)])]
        ).refinancings[0]
        # Half at five per cent and half at eight.
        assert event.new_rate == money("0.065")

    def test_the_saving_is_struck_on_the_balance_retired(self) -> None:
        event = run([takeout(into=[Draw.of("New TLB", 450)])]).refinancings[0]
        assert event.annual_saving == event.repaid * money("0.03")
        # Not on the larger face, which would credit the upsizing as a saving.
        assert event.annual_saving < money(450) * money("0.03")

    def test_the_remainder_is_the_periods_left_after_the_takeout(self) -> None:
        assert run([takeout(period=2)]).refinancings[0].periods_remaining == 3
        assert run([takeout(period=4)]).refinancings[0].periods_remaining == 1

    def test_the_cash_cost_excludes_the_write_off(self) -> None:
        event = run(
            [
                takeout(
                    into=[
                        Draw.of(
                            "New TLB", 400, issue_price="0.99", financing_fee_rate="0.01"
                        )
                    ],
                    unamortised_fees=30,
                )
            ]
        ).refinancings[0]
        assert event.cash_cost == event.call_premium + money(4) + money(4)
        assert event.fees_written_off == money(30)

    def test_a_takeout_early_enough_pays_back(self) -> None:
        event = run([takeout(period=1, into=[Draw.of("New TLB", 430)])]).refinancings[0]
        assert event.periods_remaining == 4
        assert event.saving_over_the_remainder > event.cash_cost
        assert event.pays_back

    def test_a_premium_large_enough_is_never_earned_back(self) -> None:
        """Three points of spread over one period against five points of premium."""
        event = run([takeout(period=4, call_premium_rate="0.05")]).refinancings[0]
        assert event.periods_remaining == 1
        assert event.spread_saved == money("0.03")
        assert event.saving_over_the_remainder < event.cash_cost
        assert not event.pays_back

    def test_refinancing_into_dearer_paper_saves_nothing(self) -> None:
        expensive = CapitalStructure.of(
            [
                Tranche.of("Old TLB", TrancheKind.TERM_LOAN, 500, cash_rate="0.05", floating=False),
                Tranche.of("New TLB", TrancheKind.TERM_LOAN, 0, cash_rate="0.09", floating=False),
            ],
            minimum_cash=20,
        )
        periods = list(PeriodGrid.build(CLOSE, years=5, frequency=Frequency.ANNUAL))
        schedule = DebtSchedule.run(
            expensive,
            periods,
            [money(90)] * len(periods),
            opening_cash=60,
            refinancings=[takeout()],
        )
        event = schedule.refinancings[0]
        assert event.spread_saved == money("-0.04")
        assert event.annual_saving < 0
        assert not event.pays_back


def deal_with(**blocks: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8},
        "debt": [
            {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.07},
            {"name": "New TLB", "face": 0, "kind": "term_loan", "cash_rate": 0.04},
        ],
        "rollover_equity": 100,
        "structure": {"minimum_cash": 20, "base_rate": 0.03},
        "projection": {"years": 5, "frequency": "annual"},
        "operating": {
            "opening_revenue": 1000,
            "revenue_growth": 0.05,
            "ebitda_margin": 0.22,
            "da_rate": 0.03,
            "capex_rate": 0.035,
            "nwc_rate": 0.1,
            "tax_rate": 0.25,
        },
    }
    payload.update(blocks)
    return payload


def one(**fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "period": 2,
        "tranche": "TLB",
        "into": [{"tranche": "New TLB", "amount": 850}],
    }
    event.update(fields)
    return event


class TestReadingTheEvent:
    def test_the_fields_reach_the_event(self) -> None:
        deal = parse_deal(
            deal_with(
                refinancings=[
                    one(
                        label="Repricing",
                        call_premium_rate=0.015,
                        unamortised_fees=9,
                    )
                ]
            )
        )
        event = deal.refinancings[0]
        assert deal.has_refinancings
        assert event.label == "Repricing"
        assert event.tranche == "TLB"
        assert event.call_premium_rate == money("0.015")
        assert event.unamortised_fees == money(9)
        assert event.face == money(850)

    def test_the_default_label(self) -> None:
        deal = parse_deal(deal_with(refinancings=[one()]))
        assert deal.refinancings[0].label == "Refinancing"

    def test_a_block_that_is_not_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of events"):
            parse_deal(deal_with(refinancings={"period": 2}))

    def test_an_event_that_is_not_an_object(self) -> None:
        with pytest.raises(DealSpecError, match=r"refinancings\[0\]: expected an object"):
            parse_deal(deal_with(refinancings=["a takeout"]))

    def test_an_event_with_no_period(self) -> None:
        with pytest.raises(DealSpecError, match="say which period"):
            parse_deal(deal_with(refinancings=[{"tranche": "TLB"}]))

    def test_an_event_with_no_facility(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'tranche'"):
            parse_deal(deal_with(refinancings=[{"period": 2}]))

    def test_new_paper_that_is_not_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of take-downs"):
            parse_deal(deal_with(refinancings=[one(into={"amount": 10})]))

    def test_a_draw_error_names_the_block_it_came_from(self) -> None:
        with pytest.raises(DealSpecError, match=r"refinancings\[0\]\.into\[0\]"):
            parse_deal(deal_with(refinancings=[one(into=[{"tranche": "New TLB"}])]))

    def test_an_event_without_a_structure(self) -> None:
        payload = deal_with(refinancings=[one()])
        del payload["debt"]
        del payload["structure"]
        with pytest.raises(DealSpecError, match="nothing to take out"):
            parse_deal(payload)

    def test_an_event_without_a_projection(self) -> None:
        payload = deal_with(refinancings=[one()])
        del payload["projection"]
        with pytest.raises(DealSpecError, match="'projection' is missing"):
            parse_deal(payload)

    def test_a_schedule_error_reaches_the_caller_as_a_spec_error(self) -> None:
        deal = parse_deal(deal_with(refinancings=[one(period=9)]))
        with pytest.raises(DealSpecError, match="refinancings: .*beyond the 5 periods"):
            deal.schedule()


class TestThornbury:
    def test_the_example_runs_end_to_end(self) -> None:
        deal = load_deal(THORNBURY)
        schedule = deal.schedule()
        assert deal.has_refinancings
        assert len(schedule.refinancings) == 1
        for row in schedule:
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()

    def test_the_old_facility_is_gone_from_the_period_it_was_retired(self) -> None:
        schedule = load_deal(THORNBURY).schedule()
        assert schedule[3].tranche("Term Loan B").closing == ZERO
        assert schedule[3].tranche("Repriced term loan").closing == money(195)

    def test_the_covenants_still_hold(self) -> None:
        assert load_deal(THORNBURY).test_covenants().passes

    def test_the_repricing_does_not_earn_back_its_cost(self) -> None:
        """One period left is not enough, and the memo has to say so."""
        event = load_deal(THORNBURY).schedule().refinancings[0]
        assert event.periods_remaining == 1
        assert event.spread_saved > 0
        assert event.saving_over_the_remainder < event.cash_cost
        assert not event.pays_back

    def test_taking_the_takeout_out_leaves_the_deal_slightly_better(self) -> None:
        deal = load_deal(THORNBURY)
        without = dataclasses.replace(deal, refinancings=())
        with_takeout, no_takeout = deal.realise().irr, without.realise().irr
        assert with_takeout is not None and no_takeout is not None
        assert with_takeout < no_takeout


class TestTheMemo:
    def test_the_report_grows_a_section(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        section = report.section("Refinanced during the hold")
        assert section.table is not None
        assert len(section.table.rows) == 1

    def test_the_verdict_is_stated_rather_than_left_to_the_reader(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        section = report.section("Refinanced during the hold")
        assert "does not earn back" in section.summary
        assert any("does not cover what it cost" in note for note in section.notes)

    def test_the_write_off_is_labelled_non_cash(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        section = report.section("Refinanced during the hold")
        line = next(l for l in section.lines if l.label == "Fees written off")
        assert "non-cash" in line.note

    def test_a_deal_with_no_takeout_has_no_such_section(self) -> None:
        report = prepare(load_deal(KESTREL), breakevens=False)
        with pytest.raises(KeyError):
            report.section("Refinanced during the hold")

    def test_the_memo_renders_in_both_forms(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        assert "Refinanced during the hold" in report.as_text()
        assert "Refinanced during the hold" in report.as_markdown()
