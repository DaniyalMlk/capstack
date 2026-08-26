"""Reading an incentive plan out of a deal file, and running a deal through it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.incentive import IncentiveError
from capstack.money import ONE, ZERO, is_close, money
from capstack.report import prepare
from capstack.sensitivity import Case, Dimension
from capstack.spec import DealSpecError, load_deal, parse_deal

KESTREL = str(Path(__file__).resolve().parents[1] / "examples" / "kestrel.json")


def deal_with(**blocks: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8},
        "debt": [{"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05}],
        "rollover_equity": 100,
        "structure": {"minimum_cash": 20, "base_rate": 0.03},
        "projection": {"years": 4, "frequency": "annual"},
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


def with_plan(**plan: Any) -> dict[str, Any]:
    return deal_with(exit={"multiple": 9, "incentive": {"share": 0.1, **plan}})


# --------------------------------------------------------------------------
# Reading the block
# --------------------------------------------------------------------------

class TestReadingThePlan:
    def test_a_deal_with_no_plan_has_no_pool(self) -> None:
        deal = parse_deal(deal_with(exit={"multiple": 9}))
        assert deal.incentive is None
        assert deal.pool is None
        assert deal.realise().incentive is None

    def test_the_share_and_a_default_name_are_read(self) -> None:
        deal = parse_deal(with_plan())
        assert deal.incentive is not None
        assert deal.incentive.share == money("0.1")
        assert deal.incentive.name == "Management incentive plan"

    def test_a_stated_strike_is_carried_through(self) -> None:
        deal = parse_deal(with_plan(strike=40))
        pool = deal.pool
        assert pool is not None
        assert pool.strike == money(40)

    def test_a_strike_at_entry_is_derived_against_the_equity(self) -> None:
        deal = parse_deal(
            deal_with(
                exit={
                    "multiple": 9,
                    "equity": [
                        {"name": "Sponsor", "of": "sponsor", "ownership": 0.8},
                        {"name": "Rollover", "of": "rollover", "ownership": 0.2},
                    ],
                    "incentive": {"share": 0.1, "strike_at_entry": 1},
                }
            )
        )
        invested = sum((s.invested for s in deal.securities), ZERO)
        assert invested > ZERO
        pool = deal.pool
        assert pool is not None
        # The pool's share of the cheque grossed up by what the pool does not hold.
        assert is_close(
            pool.strike, money("0.1") * invested / money("0.9"), tolerance="1E-12"
        )

    def test_an_undeclared_equity_stack_still_prices_the_strike(self) -> None:
        """A file with no equity block falls back to the default sponsor and rollover."""
        deal = parse_deal(with_plan(strike_at_entry=1))
        assert deal.securities == ()
        pool = deal.pool
        assert pool is not None and pool.strike > ZERO

    def test_a_strike_at_entry_moves_with_the_entry_multiple(self) -> None:
        """The whole reason the strike is a description rather than an amount."""
        cheap = parse_deal(with_plan(strike_at_entry=1))
        dear = with_plan(strike_at_entry=1)
        dear["entry"]["multiple"] = 10
        expensive = parse_deal(dear)
        assert cheap.pool is not None and expensive.pool is not None
        assert expensive.pool.strike > cheap.pool.strike

    def test_a_strike_given_twice_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="two answers"):
            parse_deal(with_plan(strike=10, strike_at_entry=1))

    def test_a_plan_with_no_share_is_refused(self) -> None:
        payload = deal_with(exit={"multiple": 9, "incentive": {"strike": 10}})
        with pytest.raises(DealSpecError, match="missing required field 'share'"):
            parse_deal(payload)

    def test_a_pool_holding_the_whole_company_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="below 1"):
            parse_deal(deal_with(exit={"multiple": 9, "incentive": {"share": 1}}))

    def test_a_plan_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="expected an object"):
            parse_deal(deal_with(exit={"multiple": 9, "incentive": [1, 2]}))

    def test_a_negative_strike_at_entry_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="not a strike"):
            parse_deal(with_plan(strike_at_entry=-1))


class TestReadingTheVesting:
    def test_a_bare_number_is_a_straight_line_vest(self) -> None:
        deal = parse_deal(with_plan(vesting=4))
        pool = deal.pool
        assert pool is not None and pool.vesting is not None
        assert pool.vesting.years == money(4)
        assert pool.vesting.cliff_years == ZERO
        assert not pool.vesting.accelerates

    def test_an_object_carries_the_cliff_and_the_acceleration(self) -> None:
        deal = parse_deal(
            with_plan(vesting={"years": 4, "cliff_years": 1, "accelerates": True})
        )
        pool = deal.pool
        assert pool is not None and pool.vesting is not None
        assert pool.vesting.cliff_years == money(1)
        assert pool.vesting.accelerates

    def test_a_vest_with_no_years_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'years'"):
            parse_deal(with_plan(vesting={"cliff_years": 1}))

    def test_a_cliff_past_the_schedule_is_refused_when_the_file_is_read(self) -> None:
        with pytest.raises(DealSpecError, match="never lapses"):
            parse_deal(with_plan(vesting={"years": 3, "cliff_years": 4}))


class TestReadingTheRatchet:
    def test_bands_are_read_as_pairs(self) -> None:
        deal = parse_deal(with_plan(ratchet={"bands": [[0, 0.05], [2, 0.1]]}))
        pool = deal.pool
        assert pool is not None and pool.ratchet is not None
        assert [b.hurdle for b in pool.ratchet] == [ZERO, money(2)]
        assert pool.ratchet.top_share == money("0.1")

    def test_the_instruments_watched_are_read(self) -> None:
        deal = parse_deal(
            with_plan(
                ratchet={"bands": [[0, 0.05]], "measured_on": ["Sponsor equity"]}
            )
        )
        pool = deal.pool
        assert pool is not None and pool.ratchet is not None
        assert pool.ratchet.measured_on == ("Sponsor equity",)

    def test_a_ratchet_with_no_bands_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="list of \\[hurdle, share\\] pairs"):
            parse_deal(with_plan(ratchet={"bands": []}))

    def test_a_band_that_is_not_a_pair_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match=r"bands\[1\]"):
            parse_deal(with_plan(ratchet={"bands": [[0, 0.05], [2]]}))

    def test_bands_not_starting_at_zero_are_refused(self) -> None:
        with pytest.raises(DealSpecError, match="start the"):
            parse_deal(with_plan(ratchet={"bands": [[2, 0.05]]}))

    def test_measured_on_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="list of security names"):
            parse_deal(with_plan(ratchet={"bands": [[0, 0.05]], "measured_on": "x"}))

    def test_watching_an_instrument_that_is_not_there_is_refused(self) -> None:
        payload = with_plan(ratchet={"bands": [[0, 0.05]], "measured_on": ["Founder"]})
        deal = parse_deal(payload)
        with pytest.raises(DealSpecError, match="not in the equity"):
            deal.realise()


# --------------------------------------------------------------------------
# Running a deal through the plan
# --------------------------------------------------------------------------

class TestThePlanInADeal:
    def test_the_plan_takes_from_the_common_and_nothing_else_moves(self) -> None:
        without = parse_deal(deal_with(exit={"multiple": 9})).realise()
        with_pool = parse_deal(with_plan()).realise()

        assert with_pool.valuation.equity_value == without.valuation.equity_value
        assert with_pool.proceeds < without.proceeds
        assert with_pool.incentive is not None
        assert is_close(
            without.proceeds - with_pool.proceeds,
            with_pool.incentive.paid,
            tolerance="1E-12",
        )

    def test_what_management_are_paid_is_what_the_common_give_up(self) -> None:
        for share, strike in (("0.05", 0), ("0.1", 25), ("0.2", 120)):
            outcome = parse_deal(
                deal_with(
                    exit={"multiple": 9, "incentive": {"share": share, "strike": strike}}
                )
            ).realise()
            assert outcome.incentive is not None
            assert outcome.incentive.reconciles()

    def test_the_equity_value_is_still_fully_distributed(self) -> None:
        outcome = parse_deal(with_plan(strike_at_entry=1)).realise()
        assert outcome.distributes_everything
        assert is_close(
            outcome.distributed, outcome.valuation.equity_value, tolerance="1E-12"
        )

    def test_a_deal_with_no_plan_distributes_the_same_way_it_always_did(self) -> None:
        outcome = parse_deal(deal_with(exit={"multiple": 9})).realise()
        assert outcome.incentive_paid == ZERO
        assert outcome.distributed == outcome.proceeds
        assert outcome.distributes_everything

    def test_an_unvested_plan_costs_the_sponsor_nothing(self) -> None:
        """A five-year cliff on a four-year hold: nothing has been earned."""
        without = parse_deal(deal_with(exit={"multiple": 9})).realise()
        with_pool = parse_deal(
            with_plan(vesting={"years": 6, "cliff_years": 5})
        ).realise()
        assert with_pool.incentive is not None
        assert with_pool.incentive.vested == ZERO
        assert with_pool.proceeds == without.proceeds

    def test_acceleration_pays_a_plan_the_schedule_would_not_have(self) -> None:
        cliffed = parse_deal(with_plan(vesting={"years": 9, "cliff_years": 8}))
        accelerated = parse_deal(
            with_plan(vesting={"years": 9, "cliff_years": 8, "accelerates": True})
        )
        lapsed = cliffed.realise().incentive
        paid = accelerated.realise().incentive
        assert lapsed is not None and paid is not None
        assert lapsed.paid == ZERO
        assert paid.paid > ZERO
        assert paid.vested == ONE


class TestKestrel:
    """The worked example that has a plan in it."""

    def test_the_file_parses_and_the_plan_is_read(self) -> None:
        deal = load_deal(KESTREL)
        pool = deal.pool
        assert pool is not None
        assert pool.share == money("0.1")
        assert pool.ratchet is not None
        assert pool.ratchet.measured_on == ("Sponsor equity",)

    def test_the_sponsor_clears_the_first_hurdle_and_not_the_second(self) -> None:
        outcome = load_deal(KESTREL).realise()
        sponsor = outcome.security("Sponsor equity")
        assert sponsor.moic is not None
        assert money(2) < sponsor.moic < money("2.5")

    def test_the_blended_share_sits_between_the_two_bands_in_play(self) -> None:
        settled = load_deal(KESTREL).realise().incentive
        assert settled is not None
        assert money("0.05") < settled.effective_share < money("0.1")

    def test_the_plan_ties_back_to_the_equity_value(self) -> None:
        outcome = load_deal(KESTREL).realise()
        assert outcome.distributes_everything

    def test_the_hurdle_binds_where_the_solver_says_it_does(self) -> None:
        """Re-derive the first band's boundary and check the sponsor lands on 2.0x."""
        outcome = load_deal(KESTREL).realise()
        settled = outcome.incentive
        assert settled is not None
        ratchet = settled.pool.ratchet
        assert ratchet is not None

        sponsor = outcome.security("Sponsor equity")
        opens = ratchet.boundaries(
            measured_capital=sponsor.invested,
            measured_prior=ZERO,
            measured_ownership=sponsor.security.ownership,
        )
        at_hurdle = opens[1]
        entitlement = ratchet.entitlement(
            at_hurdle,
            measured_capital=sponsor.invested,
            measured_prior=ZERO,
            measured_ownership=sponsor.security.ownership,
        )
        sponsor_proceeds = (at_hurdle - entitlement) * sponsor.security.ownership
        assert is_close(sponsor_proceeds / sponsor.invested, money(2), tolerance="1E-12")

    def test_the_pot_the_deal_reached_is_above_that_boundary(self) -> None:
        outcome = load_deal(KESTREL).realise()
        settled = outcome.incentive
        assert settled is not None
        ratchet = settled.pool.ratchet
        assert ratchet is not None
        sponsor = outcome.security("Sponsor equity")
        opens = ratchet.boundaries(
            measured_capital=sponsor.invested,
            measured_prior=ZERO,
            measured_ownership=sponsor.security.ownership,
        )
        assert settled.pot > opens[1]
        assert settled.pot < opens[2]


# --------------------------------------------------------------------------
# What comes out the other end
# --------------------------------------------------------------------------

class TestPrinting:
    def test_the_exit_subcommand_prints_the_plan(self, capsys: Any) -> None:
        assert main(["exit", KESTREL]) == 0
        out = capsys.readouterr().out
        assert "Management incentive plan" in out
        assert "Paid to management" in out
        assert "Share of the pot" in out

    def test_the_json_carries_the_plan(self, capsys: Any) -> None:
        assert main(["exit", KESTREL, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        block = payload["incentive"]
        assert block["exercised"] is True
        assert 0.05 < block["effective_share"] < 0.1
        assert payload["totals"]["distributed"] is not None

    def test_a_deal_without_a_plan_reports_none_rather_than_zero(
        self, capsys: Any
    ) -> None:
        example = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")
        assert main(["exit", example, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["incentive"] is None

    def test_the_memo_grows_a_section(self) -> None:
        report = prepare(load_deal(KESTREL), breakevens=False)
        section = report.section("Management incentive plan")
        assert "Paid to management" in [line.label for line in section.lines]
        assert section.table is not None
        assert section.table.headings == ("Above", "Marginal share")

    def test_the_memo_has_no_such_section_without_a_plan(self) -> None:
        example = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")
        report = prepare(load_deal(example), breakevens=False)
        with pytest.raises(KeyError):
            report.section("Management incentive plan")

    def test_an_expired_plan_says_so(self, capsys: Any) -> None:
        deal = parse_deal(with_plan(strike_at_entry=8))
        settled = deal.realise().incentive
        assert settled is not None
        assert not settled.exercised
        report = prepare(deal, breakevens=False)
        section = report.section("Management incentive plan")
        assert "pays nothing" in section.summary
        assert any("out of the money" in note.lower() for note in section.notes)


class TestPlanErrorsSurfaceAsSpecErrors:
    def test_a_ratchet_watching_nothing_reports_the_stack_it_looked_at(self) -> None:
        deal = parse_deal(
            with_plan(ratchet={"bands": [[0, 0.05]], "measured_on": ["Ghost"]})
        )
        with pytest.raises(DealSpecError) as caught:
            deal.realise()
        assert "Sponsor equity" in str(caught.value)

    def test_the_underlying_error_is_an_incentive_error(self) -> None:
        deal = parse_deal(
            with_plan(ratchet={"bands": [[0, 0.05]], "measured_on": ["Ghost"]})
        )
        with pytest.raises(DealSpecError) as caught:
            deal.realise()
        assert isinstance(caught.value.__cause__, IncentiveError)


# --------------------------------------------------------------------------
# Across a grid
# --------------------------------------------------------------------------

class TestAcrossASensitivityGrid:
    """The reason the plan is a description rather than a settled amount."""

    def test_the_strike_is_repriced_when_the_entry_multiple_moves(self) -> None:
        deal = load_deal(KESTREL)
        dearer = Dimension.ENTRY_MULTIPLE.apply(deal, money("11.5"))

        base = deal.pool
        moved = dearer.pool
        assert base is not None and moved is not None
        # A higher price is a bigger sponsor cheque, so the options that were
        # struck at what the equity cost now cost more to exercise.
        assert moved.strike > base.strike

    def test_a_grid_reprices_the_plan_per_cell_rather_than_once(self) -> None:
        deal = load_deal(KESTREL)
        strikes = set()
        for multiple in ("9.00", "9.75", "10.50"):
            cell = Dimension.ENTRY_MULTIPLE.apply(deal, money(multiple))
            pool = cell.pool
            assert pool is not None
            strikes.add(pool.strike)
        assert len(strikes) == 3

    def test_every_cell_still_distributes_its_whole_equity_value(self) -> None:
        deal = load_deal(KESTREL)
        for multiple in ("8.50", "9.75", "11.00"):
            case = Case.run(Dimension.EXIT_MULTIPLE.apply(deal, money(multiple)))
            assert case.outcome.distributes_everything
            settled = case.outcome.incentive
            assert settled is not None
            assert settled.reconciles()

    def test_the_plan_costs_less_the_worse_the_exit(self) -> None:
        """Struck options are the whole point: a bad exit costs the sponsor nothing."""
        deal = load_deal(KESTREL)
        paid = []
        for multiple in ("6.00", "8.00", "10.00", "12.00"):
            case = Case.run(Dimension.EXIT_MULTIPLE.apply(deal, money(multiple)))
            settled = case.outcome.incentive
            assert settled is not None
            paid.append(settled.paid)
        assert paid == sorted(paid)
        assert paid[0] == ZERO
