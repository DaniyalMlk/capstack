"""Raising debt mid-hold, paying it out, and what that does to a return."""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.debt import (
    CapitalStructure,
    DebtSchedule,
    Tranche,
    TrancheKind,
)
from capstack.events import Draw, Recapitalisation, RecapitalisationError
from capstack.money import ONE, ZERO, is_close, money
from capstack.periods import Frequency, PeriodGrid
from capstack.report import prepare
from capstack.spec import DealSpecError, load_deal, parse_deal

KESTREL = str(Path(__file__).resolve().parents[1] / "examples" / "kestrel.json")

CLOSE = date(2026, 6, 30)


def structure(minimum_cash: int = 20) -> CapitalStructure:
    return CapitalStructure.of(
        [
            Tranche.of(
                "TLB",
                TrancheKind.TERM_LOAN,
                500,
                cash_rate="0.06",
                floating=False,
            )
        ],
        minimum_cash=minimum_cash,
    )


def grid(years: int = 5) -> list[Any]:
    return list(PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL))


def run(
    events: list[Recapitalisation] | None = None,
    *,
    flows: int = 90,
    opening_cash: int = 30,
    minimum_cash: int = 20,
) -> DebtSchedule:
    periods = grid()
    return DebtSchedule.run(
        structure(minimum_cash),
        periods,
        [money(flows)] * len(periods),
        opening_cash=opening_cash,
        recapitalisations=events or [],
    )


# --------------------------------------------------------------------------
# The event on its own
# --------------------------------------------------------------------------

class TestTheEvent:
    def test_a_draw_nets_its_fees_and_its_discount(self) -> None:
        draw = Draw.of("TLB", 300, issue_price="0.99", financing_fee_rate="0.02")
        assert draw.gross_proceeds == money(297)
        assert draw.discount == money(3)
        assert draw.fees == money(6)
        assert draw.net_proceeds == money(291)

    def test_a_draw_at_par_with_no_fees_raises_its_face(self) -> None:
        assert Draw.of("TLB", 300).net_proceeds == money(300)

    def test_the_distribution_is_the_net_raise_plus_the_cash(self) -> None:
        event = Recapitalisation.of(
            3, [Draw.of("TLB", 300, issue_price="0.99", financing_fee_rate="0.02")],
            from_cash=15,
        )
        assert event.face == money(300)
        assert event.distribution == money(306)

    def test_a_draw_of_nothing_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="raises nothing"):
            Draw.of("TLB", 0)

    def test_an_unnamed_tranche_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="name the tranche"):
            Draw.of("  ", 100)

    def test_an_issue_price_of_zero_is_not_a_price(self) -> None:
        with pytest.raises(RecapitalisationError, match="not a price"):
            Draw.of("TLB", 100, issue_price=0)

    def test_a_fee_of_everything_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="cost more than the draw"):
            Draw.of("TLB", 100, financing_fee_rate=1)

    def test_periods_are_numbered_from_one(self) -> None:
        with pytest.raises(RecapitalisationError, match="numbered from one"):
            Recapitalisation.of(0, [Draw.of("TLB", 100)])

    def test_an_event_that_does_nothing_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="raises nothing and pays"):
            Recapitalisation.of(2)

    def test_the_same_tranche_twice_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="drawn once"):
            Recapitalisation.of(2, [Draw.of("TLB", 100), Draw.of("TLB", 50)])

    def test_a_contribution_is_not_a_recapitalisation(self) -> None:
        with pytest.raises(RecapitalisationError, match="a contribution"):
            Recapitalisation.of(2, [Draw.of("TLB", 100)], from_cash=-5)

    def test_cash_alone_is_a_recapitalisation(self) -> None:
        """A dividend out of the balance sheet raises no debt but still pays out."""
        event = Recapitalisation.of(2, from_cash=25)
        assert event.face == ZERO
        assert event.distribution == money(25)


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------

class TestTheSchedule:
    def test_the_new_face_lands_on_the_tranche_at_the_period_end(self) -> None:
        schedule = run([Recapitalisation.of(3, [Draw.of("TLB", 200)])])
        third = schedule[2]
        assert third.tranche("TLB").recapitalisation == money(200)
        assert schedule.total_recapitalised == money(200)

    def test_the_next_period_opens_on_the_larger_balance(self) -> None:
        schedule = run([Recapitalisation.of(3, [Draw.of("TLB", 200)])])
        assert schedule[3].opening_debt == schedule[2].closing_debt

    def test_a_period_before_the_event_is_untouched(self) -> None:
        base = run()
        recap = run([Recapitalisation.of(4, [Draw.of("TLB", 200)])])
        for i in range(3):
            assert base[i].closing_debt == recap[i].closing_debt
            assert base[i].closing_cash == recap[i].closing_cash

    def test_the_roll_forward_still_closes_on_every_row(self) -> None:
        schedule = run([Recapitalisation.of(3, [Draw.of("TLB", 200)], from_cash=10)])
        assert all(p.reconciles() for p in schedule)
        assert all(t.reconciles() for p in schedule for t in p.tranches)

    def test_cash_taken_off_the_balance_sheet_leaves_it(self) -> None:
        base = run()
        recap = run([Recapitalisation.of(3, from_cash=10)])
        assert recap[2].closing_cash == base[2].closing_cash - money(10)
        assert recap[2].distribution_from_cash == money(10)
        assert recap[2].distribution == money(10)

    def test_proceeds_never_touch_the_balance_sheet(self) -> None:
        """The raise is paid straight out, so cash at the event is unchanged."""
        base = run()
        recap = run([Recapitalisation.of(3, [Draw.of("TLB", 200)])])
        assert recap[2].closing_cash == base[2].closing_cash
        assert recap[2].distribution == money(200)

    def test_the_new_debt_costs_interest_from_the_following_period(self) -> None:
        base = run()
        recap = run([Recapitalisation.of(3, [Draw.of("TLB", 200)])])
        assert recap[2].cash_interest == base[2].cash_interest
        assert recap[3].cash_interest > base[3].cash_interest

    def test_the_distributions_carry_their_dates(self) -> None:
        schedule = run([Recapitalisation.of(3, [Draw.of("TLB", 200)])])
        paid = schedule.distributions()
        assert len(paid) == 1
        period, amount = paid[0]
        assert period.index == 3
        assert amount == money(200)
        assert schedule.total_distributed == money(200)

    def test_a_deal_with_no_event_distributes_nothing(self) -> None:
        schedule = run()
        assert schedule.distributions() == ()
        assert schedule.total_distributed == ZERO
        assert schedule.recapitalisations == ()

    def test_the_event_is_recorded_with_the_leverage_either_side(self) -> None:
        periods = grid()
        schedule = DebtSchedule.run(
            structure(),
            periods,
            [money(90)] * len(periods),
            opening_cash=30,
            ebitda=[money(120)] * len(periods),
            recapitalisations=[Recapitalisation.of(3, [Draw.of("TLB", 240)])],
        )
        (event,) = schedule.recapitalisations
        assert event.leverage_before is not None
        assert event.leverage_after is not None
        # 240 of new face over 120 of EBITDA is two turns.
        assert is_close(event.turns_added or ZERO, money(2), tolerance="1E-9")


class TestWhatIsRefused:
    def test_cash_below_the_minimum_is_refused_rather_than_clamped(self) -> None:
        with pytest.raises(RecapitalisationError, match="leaves only"):
            run([Recapitalisation.of(1, from_cash=10_000)])

    def test_the_message_carries_the_numbers(self) -> None:
        with pytest.raises(RecapitalisationError) as caught:
            run([Recapitalisation.of(1, from_cash=10_000)], minimum_cash=20)
        assert "minimum of 20" in str(caught.value)

    def test_taking_exactly_the_headroom_is_allowed(self) -> None:
        base = run()
        available = base[0].closing_cash - money(20)
        schedule = run([Recapitalisation.of(1, from_cash=int(available))])
        assert schedule[0].closing_cash >= money(20)

    def test_an_event_beyond_the_schedule_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="beyond the 5 periods"):
            run([Recapitalisation.of(9, [Draw.of("TLB", 100)])])

    def test_two_events_in_one_period_are_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="combine them"):
            run(
                [
                    Recapitalisation.of(3, [Draw.of("TLB", 100)]),
                    Recapitalisation.of(3, from_cash=5),
                ]
            )

    def test_drawing_on_a_tranche_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(RecapitalisationError, match="no tranche named"):
            run([Recapitalisation.of(3, [Draw.of("Mezzanine", 100)])])

    def test_drawing_on_a_matured_tranche_is_refused(self) -> None:
        periods = grid()
        matured = CapitalStructure.of(
            [
                Tranche.of(
                    "TLB",
                    TrancheKind.TERM_LOAN,
                    500,
                    cash_rate="0.06",
                    floating=False,
                    maturity=2,
                )
            ],
            minimum_cash=20,
        )
        with pytest.raises(RecapitalisationError, match="has matured"):
            DebtSchedule.run(
                matured,
                periods,
                [money(90)] * len(periods),
                opening_cash=30,
                recapitalisations=[Recapitalisation.of(4, [Draw.of("TLB", 100)])],
            )


# --------------------------------------------------------------------------
# The waterfall, run more than once
# --------------------------------------------------------------------------

def deal_with(**blocks: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8},
        "debt": [{"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05}],
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


PREFERRED_EQUITY = [
    {
        "name": "Sponsor preferred",
        "kind": "preferred",
        "of": "sponsor",
        "share": 0.9,
        "preferred_rate": 0.08,
    },
    {
        "name": "Sponsor common",
        "kind": "common",
        "of": "sponsor",
        "share": 0.1,
        "ownership": 0.8,
    },
    {
        "name": "Rollover",
        "kind": "common",
        "of": "rollover",
        "ownership": 0.2,
    },
]


def recap_deal(period: int = 3, amount: int = 150, **extra: Any) -> Any:
    return parse_deal(
        deal_with(
            exit={"multiple": 8, "equity": PREFERRED_EQUITY},
            recapitalisations=[
                {"period": period, "draws": [{"tranche": "TLB", "amount": amount}]}
            ],
            **extra,
        )
    )


class TestTheWaterfallRunsTwice:
    def test_every_payment_is_fully_distributed(self) -> None:
        outcome = recap_deal().realise()
        assert len(outcome.payments) == 2
        assert all(p.reconciles() for p in outcome.payments)

    def test_the_exit_payment_is_the_last_one(self) -> None:
        outcome = recap_deal().realise()
        assert outcome.payments[-1].is_exit
        assert not outcome.payments[0].is_exit
        assert len(outcome.distributions) == 1

    def test_the_preferred_takes_the_interim_distribution_first(self) -> None:
        """A preferred claim ranks ahead of the common at every payment, not just the last."""
        outcome = recap_deal().realise()
        interim = outcome.distributions[0]
        preferred = outcome.security("Sponsor preferred")
        index = [r.name for r in outcome].index("Sponsor preferred")
        assert interim.preferred_paid[index] == interim.amount
        assert interim.to_common == ZERO
        assert preferred.interim == interim.amount

    def test_capital_repaid_early_reduces_the_claim_at_exit(self) -> None:
        flat = parse_deal(
            deal_with(exit={"multiple": 8, "equity": PREFERRED_EQUITY})
        ).realise()
        recapped = recap_deal().realise()

        before = flat.security("Sponsor preferred")
        after = recapped.security("Sponsor preferred")
        assert after.capital_repaid_early > ZERO
        assert after.standing_capital < before.standing_capital
        assert after.claim < before.claim

    def test_the_coupon_runs_on_what_is_left_and_not_on_the_cheque(self) -> None:
        """Re-derive the preferred's claim at exit by hand from the reduced base."""
        outcome = recap_deal().realise()
        preferred = outcome.security("Sponsor preferred")
        security = preferred.security
        interim = outcome.distributions[0]
        index = [r.name for r in outcome].index("Sponsor preferred")

        # The claim standing when the distribution arrived.
        first = security.invested + security.accrued_at(interim.years)
        paid = interim.preferred_paid[index]
        # Accrued return is met before capital, so what is left is capital less
        # whatever the payment reached past the accrued return.
        standing = first - paid

        remaining = outcome.holding_period_years - interim.years
        grown = standing * money(
            repr(float(ONE + security.preferred_rate) ** float(remaining))
        )
        assert is_close(preferred.claim, grown, tolerance="1E-6")

    def test_an_interim_distribution_lifts_the_rate_and_not_the_multiple(self) -> None:
        """The whole point of the exercise, on a case run both ways."""
        flat = parse_deal(
            deal_with(exit={"multiple": 8, "equity": PREFERRED_EQUITY})
        ).realise()
        recapped = recap_deal().realise()

        assert recapped.irr is not None and flat.irr is not None
        assert recapped.irr > flat.irr
        # The multiple falls slightly: the new debt costs interest and the exit
        # equity value carries it.
        assert recapped.moic is not None and flat.moic is not None
        assert recapped.moic < flat.moic
        assert flat.moic - recapped.moic < money("0.2")

    def test_the_multiple_counts_everything_received(self) -> None:
        outcome = recap_deal().realise()
        for row in outcome:
            assert row.received == row.proceeds + row.interim
            if row.invested > 0:
                assert row.moic is not None
                assert is_close(
                    row.moic, row.received / row.invested, tolerance="1E-12"
                )

    def test_the_exit_still_distributes_the_whole_equity_value(self) -> None:
        outcome = recap_deal().realise()
        assert outcome.distributes_everything

    def test_a_deal_without_an_event_behaves_exactly_as_before(self) -> None:
        outcome = parse_deal(
            deal_with(exit={"multiple": 8, "equity": PREFERRED_EQUITY})
        ).realise()
        assert not outcome.was_recapitalised
        assert outcome.distributions == ()
        assert outcome.interim == ZERO
        assert outcome.received == outcome.proceeds
        for row in outcome:
            assert row.interim == ZERO
            assert row.capital_repaid_early == ZERO

    def test_two_events_are_both_paid(self) -> None:
        deal = parse_deal(
            deal_with(
                exit={"multiple": 8, "equity": PREFERRED_EQUITY},
                recapitalisations=[
                    {"period": 2, "draws": [{"tranche": "TLB", "amount": 80}]},
                    {"period": 4, "draws": [{"tranche": "TLB", "amount": 60}]},
                ],
            )
        )
        outcome = deal.realise()
        assert len(outcome.distributions) == 2
        assert outcome.interim == money(140)
        assert [d.years for d in outcome.distributions] == sorted(
            d.years for d in outcome.distributions
        )

    def test_the_earlier_the_payment_the_better_the_rate(self) -> None:
        early = recap_deal(period=2).realise()
        late = recap_deal(period=4).realise()
        assert early.irr is not None and late.irr is not None
        assert early.irr > late.irr


# --------------------------------------------------------------------------
# The deal file
# --------------------------------------------------------------------------

class TestReadingTheEvent:
    def test_the_block_is_read(self) -> None:
        deal = recap_deal()
        assert deal.has_recapitalisations
        (event,) = deal.recapitalisations
        assert event.period == 3
        assert event.draw_on("TLB") == money(150)

    def test_a_label_survives_into_the_report(self) -> None:
        deal = parse_deal(
            deal_with(
                exit={"multiple": 8},
                recapitalisations=[
                    {
                        "period": 3,
                        "label": "Special dividend",
                        "draws": [{"tranche": "TLB", "amount": 100}],
                    }
                ],
            )
        )
        (event,) = deal.recapitalisations
        assert event.label == "Special dividend"

    def test_an_event_needs_a_period(self) -> None:
        with pytest.raises(DealSpecError, match="numbered from one"):
            parse_deal(
                deal_with(recapitalisations=[{"draws": [{"tranche": "TLB", "amount": 1}]}])
            )

    def test_a_draw_needs_a_tranche(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'tranche'"):
            parse_deal(deal_with(recapitalisations=[{"period": 2, "draws": [{"amount": 1}]}]))

    def test_the_list_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of events"):
            parse_deal(deal_with(recapitalisations={"period": 2}))

    def test_an_event_without_a_structure_is_refused(self) -> None:
        payload = deal_with(recapitalisations=[{"period": 2, "from_cash": 5}])
        del payload["structure"]
        with pytest.raises(DealSpecError, match="nothing to draw on"):
            parse_deal(payload)

    def test_an_event_without_a_projection_is_refused(self) -> None:
        payload = deal_with(recapitalisations=[{"period": 2, "from_cash": 5}])
        del payload["projection"]
        del payload["operating"]
        with pytest.raises(DealSpecError, match="a projection"):
            parse_deal(payload)

    def test_an_impossible_event_surfaces_as_a_spec_error(self) -> None:
        deal = parse_deal(
            deal_with(
                recapitalisations=[
                    {"period": 2, "draws": [{"tranche": "Nowhere", "amount": 10}]}
                ]
            )
        )
        with pytest.raises(DealSpecError, match="no tranche named"):
            deal.schedule()


class TestKestrel:
    def test_the_example_pays_a_dividend(self) -> None:
        outcome = load_deal(KESTREL).realise()
        assert outcome.was_recapitalised
        assert outcome.interim > ZERO

    def test_the_covenants_still_hold_after_the_raise(self) -> None:
        deal = load_deal(KESTREL)
        report = deal.test_covenants()
        assert report.breaches == ()

    def test_the_raise_costs_multiple_and_buys_rate(self) -> None:
        deal = load_deal(KESTREL)
        flat = dataclasses.replace(deal, recapitalisations=())
        run_with, run_without = deal.realise(), flat.realise()
        assert run_with.irr is not None and run_without.irr is not None
        assert run_with.moic is not None and run_without.moic is not None
        assert run_with.irr > run_without.irr
        assert run_with.moic < run_without.moic

    def test_every_payment_reconciles(self) -> None:
        outcome = load_deal(KESTREL).realise()
        assert all(p.reconciles() for p in outcome.payments)


class TestPrinting:
    def test_the_exit_table_grows_an_interim_column(self, capsys: Any) -> None:
        assert main(["exit", KESTREL]) == 0
        out = capsys.readouterr().out
        assert "during hold" in out
        assert "Paid during the hold" in out

    def test_the_table_without_an_event_is_unchanged(self, capsys: Any) -> None:
        example = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")
        assert main(["exit", example]) == 0
        out = capsys.readouterr().out
        assert "during hold" not in out
        assert "Paid during the hold" not in out

    def test_the_multiple_ties_to_the_received_column(self, capsys: Any) -> None:
        """The reason the column exists: the ratio has to be checkable on the page."""
        assert main(["exit", KESTREL, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        for row in payload["securities"]:
            received = float(row["received"])
            invested = float(row["invested"])
            # The amounts are serialised at cent scale, so the ratio a reader
            # computes off the page agrees to the rounding and not beyond it.
            assert abs(row["moic"] - received / invested) < 1e-4

    def test_the_json_carries_the_distributions(self, capsys: Any) -> None:
        assert main(["exit", KESTREL, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["distributions"]) == 1
        assert payload["distributions"][0]["years"] == pytest.approx(3.0, abs=0.01)

    def test_the_memo_grows_a_section_with_both_answers(self) -> None:
        report = prepare(load_deal(KESTREL), breakevens=False)
        section = report.section("Paid during the hold")
        labels = [line.label for line in section.lines]
        assert "Money multiple, held flat" in labels
        assert "Rate of return, held flat" in labels
        assert "the same money, banked earlier" in section.summary

    def test_the_memo_has_no_such_section_without_an_event(self) -> None:
        example = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")
        report = prepare(load_deal(example), breakevens=False)
        with pytest.raises(KeyError):
            report.section("Paid during the hold")
