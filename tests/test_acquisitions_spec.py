"""Acquisitions read from a deal file, and reported out of one."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.money import ZERO, money
from capstack.report import prepare
from capstack.spec import DealSpecError, load_deal, parse_deal

THORNBURY = str(Path(__file__).resolve().parents[1] / "examples" / "thornbury.json")
KESTREL = str(Path(__file__).resolve().parents[1] / "examples" / "kestrel.json")


def deal_with(**blocks: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8},
        "debt": [
            {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05},
            {
                "name": "Acquisition facility",
                "face": 0,
                "kind": "term_loan",
                "cash_rate": 0.06,
            },
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
    purchase: dict[str, Any] = {"period": 2, "ebitda": 30, "multiple": 6}
    purchase.update(fields)
    return purchase


class TestReadingThePurchase:
    def test_the_fields_reach_the_event(self) -> None:
        deal = parse_deal(
            deal_with(
                acquisitions=[
                    one(
                        label="Bolt-on",
                        revenue=150,
                        synergies=5,
                        synergy_phase_in=3,
                        fee_rate=0.02,
                        integration_cost=2,
                        draws=[{"tranche": "Acquisition facility", "amount": 120}],
                    )
                ]
            )
        )
        add_on = deal.acquisitions[0]
        assert deal.has_acquisitions
        assert add_on.label == "Bolt-on"
        assert add_on.period == 2
        assert add_on.enterprise_value == money(180)
        assert add_on.revenue == money(150)
        assert add_on.synergies == money(5)
        assert add_on.synergy_phase_in == 3
        assert add_on.fees == money("3.6")
        assert add_on.integration_cost == money(2)
        assert add_on.face == money(120)

    def test_a_purchase_is_labelled_by_position_when_unnamed(self) -> None:
        deal = parse_deal(deal_with(acquisitions=[one(), one(period=3)]))
        assert [a.label for a in deal.acquisitions] == ["Add-on 1", "Add-on 2"]

    def test_an_own_growth_rate_is_read_as_a_driver(self) -> None:
        deal = parse_deal(
            deal_with(acquisitions=[one(growth={"ramp": [0.12, 0.04]})])
        )
        growth = deal.acquisitions[0].growth
        assert growth is not None
        assert growth.at(0) == money("0.12")
        assert growth.at(4) == money("0.04")

    def test_without_a_growth_rate_the_platforms_own_is_used(self) -> None:
        deal = parse_deal(deal_with(acquisitions=[one()]))
        assert deal.acquisitions[0].growth is None
        stream = deal.acquired_streams()[0]
        assert stream.growth is None

    def test_an_unstated_revenue_takes_the_margin_of_its_closing_period(self) -> None:
        """Not the first period's margin, and not an average of the hold."""
        deal = parse_deal(
            deal_with(
                acquisitions=[one(period=3)],
                operating=dict(
                    deal_with()["operating"],
                    ebitda_margin={"values": [0.10, 0.15, 0.20, 0.25, 0.30]},
                ),
            )
        )
        stream = deal.acquired_streams()[0]
        assert stream.margin == money("0.20")
        assert stream.revenue == money(150)

    def test_a_stated_revenue_overrides_the_platform_margin(self) -> None:
        deal = parse_deal(deal_with(acquisitions=[one(revenue=200)]))
        stream = deal.acquired_streams()[0]
        assert stream.margin == money("0.15")
        assert stream.revenue == money(200)


class TestWhatTheFileRefuses:
    def test_a_purchase_that_is_not_an_object(self) -> None:
        with pytest.raises(DealSpecError, match=r"acquisitions\[0\]: expected an object"):
            parse_deal(deal_with(acquisitions=["a business"]))

    def test_a_block_that_is_not_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of purchases"):
            parse_deal(deal_with(acquisitions={"period": 2}))

    def test_a_purchase_with_no_period(self) -> None:
        with pytest.raises(DealSpecError, match="say which period"):
            parse_deal(deal_with(acquisitions=[{"ebitda": 30, "multiple": 6}]))

    def test_a_purchase_with_no_earnings(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'ebitda'"):
            parse_deal(deal_with(acquisitions=[{"period": 2, "multiple": 6}]))

    def test_a_purchase_with_no_multiple(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'multiple'"):
            parse_deal(deal_with(acquisitions=[{"period": 2, "ebitda": 30}]))

    def test_draws_that_are_not_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of take-downs"):
            parse_deal(deal_with(acquisitions=[one(draws={"amount": 10})]))

    def test_a_draw_with_no_tranche(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'tranche'"):
            parse_deal(deal_with(acquisitions=[one(draws=[{"amount": 10}])]))

    def test_a_purchase_without_a_projection(self) -> None:
        payload = deal_with(acquisitions=[one()])
        del payload["projection"]
        del payload["operating"]
        with pytest.raises(DealSpecError, match="a projection and an operating block"):
            parse_deal(payload)

    def test_a_multiple_of_nothing(self) -> None:
        with pytest.raises(DealSpecError, match="not a price"):
            parse_deal(deal_with(acquisitions=[one(multiple=0)]))

    def test_a_purchase_in_the_final_period_reaches_the_caller_as_a_spec_error(
        self,
    ) -> None:
        deal = parse_deal(deal_with(acquisitions=[one(period=5)]))
        with pytest.raises(DealSpecError, match="nothing it buys is ever earned"):
            deal.schedule()

    def test_a_purchase_the_business_cannot_pay_for(self) -> None:
        deal = parse_deal(deal_with(acquisitions=[one(ebitda=200, multiple=10)]))
        with pytest.raises(DealSpecError, match="which leaves only"):
            deal.schedule()

    def test_an_acquisition_landing_on_a_recapitalisation(self) -> None:
        deal = parse_deal(
            deal_with(
                acquisitions=[one(period=2)],
                recapitalisations=[
                    {"period": 2, "draws": [{"tranche": "TLB", "amount": 50}]}
                ],
            )
        )
        with pytest.raises(DealSpecError, match="cannot say which happened first"):
            deal.schedule()


class TestTheBlendedEntryFromAFile:
    def test_a_deal_with_no_purchases_blends_to_its_own_multiple(self) -> None:
        deal = parse_deal(deal_with())
        blended = deal.blended_entry
        assert not deal.has_acquisitions
        assert blended.blended_multiple == money(8)
        assert blended.arbitrage == ZERO

    def test_the_platform_side_comes_from_the_entry_block(self) -> None:
        deal = parse_deal(deal_with(acquisitions=[one()]))
        blended = deal.blended_entry
        assert blended.platform_ebitda == money(200)
        assert blended.platform_enterprise_value == money(1600)
        # 1600 + 180 over 200 + 30.
        assert blended.blended_multiple == money(1780) / money(230)

    def test_a_rebuilt_deal_carries_its_purchases(self) -> None:
        """Sensitivity rebuilds a deal per cell; the programme has to survive it."""
        deal = parse_deal(deal_with(acquisitions=[one()]))
        rebuilt = dataclasses.replace(deal, exit_multiple=money(9))
        assert rebuilt.acquisitions == deal.acquisitions
        assert rebuilt.blended_entry.blended_multiple == deal.blended_entry.blended_multiple


class TestThornbury:
    def test_the_example_runs_end_to_end(self) -> None:
        deal = load_deal(THORNBURY)
        model = deal.project()
        schedule = deal.schedule(model)
        assert len(deal.acquisitions) == 3
        assert model.has_acquisitions
        assert len(schedule.acquisitions) == 3
        for row in schedule:
            assert row.reconciles()

    def test_the_programme_blends_the_entry_down_by_a_turn(self) -> None:
        deal = load_deal(THORNBURY)
        blended = deal.blended_entry
        assert blended.platform_multiple == money("10.5")
        assert blended.acquired_ebitda == money(20)
        # 504 of platform, plus 43.875, 56 and 34.375 of purchases.
        assert blended.enterprise_value == money("638.25")
        assert blended.blended_multiple == money("638.25") / money(68)
        assert blended.arbitrage > money(1)

    def test_the_covenants_hold_across_the_programme(self) -> None:
        deal = load_deal(THORNBURY)
        report = deal.test_covenants()
        assert report.passes
        assert report.first_breach is None

    def test_the_acquisitions_earn_their_place(self) -> None:
        """The counterfactual: the same deal with the purchases taken out."""
        deal = load_deal(THORNBURY)
        platform_only = dataclasses.replace(deal, acquisitions=())
        with_them = deal.realise()
        without = platform_only.realise()
        assert with_them.moic is not None and without.moic is not None
        assert with_them.moic > without.moic
        assert with_them.irr is not None and without.irr is not None
        assert with_them.irr > without.irr

    def test_a_quarter_of_the_exit_earnings_were_bought(self) -> None:
        model = load_deal(THORNBURY).project()
        share = model.exit_acquired_ebitda / model.exit_ebitda
        assert money("0.2") < share < money("0.35")


class TestTheCommandLine:
    def test_the_subcommand_prints_a_table_per_purchase(self, capsys: Any) -> None:
        assert main(["acquisitions", THORNBURY]) == 0
        out = capsys.readouterr().out
        assert "Halloway" in out
        assert "Ferrand Group" in out
        assert "Calder Services" in out
        assert "Blended multiple" in out

    def test_the_json_carries_every_purchase_and_the_blend(self, capsys: Any) -> None:
        assert main(["acquisitions", THORNBURY, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["purchases"]) == 3
        first = payload["purchases"][0]
        assert first["label"] == "Halloway"
        assert money(first["enterprise_value"]) == money("43.875")
        assert money(payload["blended"]["acquired_ebitda"]) == money(20)
        assert money(payload["blended"]["platform_multiple"]) == money("10.5")

    def test_the_funding_table_balances_in_the_json(self, capsys: Any) -> None:
        assert main(["acquisitions", THORNBURY, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        for purchase in payload["purchases"]:
            uses = money(purchase["uses"])
            sources = money(purchase["debt_proceeds"]) + money(purchase["from_cash"])
            assert uses == sources

    def test_a_deal_that_buys_nothing_says_so(self, capsys: Any) -> None:
        assert main(["acquisitions", KESTREL]) == 1
        assert "buys nothing during the hold" in capsys.readouterr().err

    def test_the_leverage_after_each_purchase_comes_from_the_schedule(
        self, capsys: Any
    ) -> None:
        assert main(["acquisitions", THORNBURY, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        schedule = load_deal(THORNBURY).schedule()
        landed = {o.event.period: o for o in schedule.acquisitions}
        for purchase in payload["purchases"]:
            outcome = landed[purchase["period"]]
            assert money(purchase["cash_after"]) == outcome.cash_after


class TestTheMemo:
    def test_the_report_grows_a_section(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        section = report.section("Bought during the hold")
        assert "arbitrage" in section.summary
        assert len(section.table.rows) == 3 if section.table else False

    def test_a_deal_that_buys_nothing_has_no_such_section(self) -> None:
        report = prepare(load_deal(KESTREL), breakevens=False)
        with pytest.raises(KeyError):
            report.section("Bought during the hold")

    def test_the_section_reports_both_readings_of_the_return(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        labels = [line.label for line in report.section("Bought during the hold").lines]
        assert "Money multiple, as run" in labels
        assert "Money multiple, platform alone" in labels
        assert "Rate of return, platform alone" in labels

    def test_the_memo_renders_in_both_forms(self) -> None:
        report = prepare(load_deal(THORNBURY), breakevens=False)
        assert "Bought during the hold" in report.as_text()
        assert "Bought during the hold" in report.as_markdown()
