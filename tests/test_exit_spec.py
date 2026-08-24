"""Reading an exit out of a deal file, and printing what comes of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.money import money
from capstack.outcome import SecurityKind
from capstack.spec import DealSpecError, parse_deal

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")


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


class TestReadingTheExit:
    def test_the_multiple_and_the_fee_are_read(self) -> None:
        deal = parse_deal(deal_with(exit={"multiple": 10, "fee_rate": 0.01}))
        assert deal.exit_multiple == money(10)
        assert deal.exit_fee_rate == money("0.01")
        assert deal.realise().valuation.multiple == money(10)

    def test_an_absent_multiple_holds_the_entry_multiple(self) -> None:
        deal = parse_deal(deal_with(exit={"fee_rate": 0.01}))
        assert deal.exit_multiple is None
        assert deal.realise().valuation.multiple == money(8)

    def test_a_deal_with_no_exit_block_still_exits_flat(self) -> None:
        deal = parse_deal(deal_with())
        outcome = deal.realise()
        assert outcome.valuation.multiple == money(8)
        assert outcome.valuation.fees == 0
        assert [r.name for r in outcome] == ["Sponsor equity", "Rollover equity"]

    def test_an_exit_needs_something_to_exit_from(self) -> None:
        payload = deal_with(exit={"multiple": 10})
        payload.pop("structure")
        with pytest.raises(DealSpecError, match="nothing to exit from"):
            parse_deal(payload)

    def test_an_exit_needs_a_close_date(self) -> None:
        payload = deal_with()
        payload.pop("close_date")
        payload["projection"] = None
        payload["operating"] = None
        deal = parse_deal(payload)
        with pytest.raises(DealSpecError, match="close date is required"):
            deal.realise()

    def test_an_exit_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="exit: expected an object"):
            parse_deal(deal_with(exit=[10]))


class TestReadingTheEquity:
    def test_a_cheque_can_be_split_between_instruments(self) -> None:
        deal = parse_deal(
            deal_with(
                exit={
                    "equity": [
                        {
                            "name": "Preferred",
                            "kind": "preferred",
                            "of": "sponsor",
                            "share": 0.9,
                            "preferred_rate": 0.08,
                        },
                        {
                            "name": "Sponsor common",
                            "of": "sponsor",
                            "share": 0.1,
                            "ownership": 0.75,
                        },
                        {
                            "name": "Rollover",
                            "of": "rollover",
                            "ownership": 0.25,
                        },
                    ]
                }
            )
        )
        preferred, common, rollover = deal.securities
        assert preferred.kind is SecurityKind.PREFERRED
        assert preferred.invested == deal.transaction.sponsor_equity * money("0.9")
        assert common.invested == deal.transaction.sponsor_equity * money("0.1")
        assert rollover.invested == money(100)
        assert preferred.invested + common.invested == deal.transaction.sponsor_equity

    def test_capital_can_be_stated_outright(self) -> None:
        deal = parse_deal(
            deal_with(exit={"equity": [{"name": "All of it", "invested": 500, "ownership": 1}]})
        )
        assert deal.securities[0].invested == money(500)

    def test_a_cheque_left_partly_unallocated_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="allocated"):
            parse_deal(
                deal_with(
                    exit={
                        "equity": [
                            {"name": "Most of it", "of": "sponsor", "share": 0.9, "ownership": 1}
                        ]
                    }
                )
            )

    def test_a_security_funded_two_ways_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="two answers to the same question"):
            parse_deal(
                deal_with(
                    exit={
                        "equity": [
                            {"name": "X", "of": "sponsor", "invested": 100, "ownership": 1}
                        ]
                    }
                )
            )

    def test_a_security_funded_no_way_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="say what funds this security"):
            parse_deal(deal_with(exit={"equity": [{"name": "X", "ownership": 1}]}))

    def test_an_unknown_cheque_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="expected sponsor or rollover"):
            parse_deal(
                deal_with(exit={"equity": [{"name": "X", "of": "lenders", "ownership": 1}]})
            )

    def test_an_unknown_kind_names_the_ones_that_exist(self) -> None:
        with pytest.raises(DealSpecError, match="unknown kind"):
            parse_deal(
                deal_with(
                    exit={
                        "equity": [
                            {"name": "X", "kind": "warrant", "of": "sponsor", "ownership": 1}
                        ]
                    }
                )
            )

    def test_an_empty_equity_list_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of securities"):
            parse_deal(deal_with(exit={"equity": []}))

    def test_ownership_that_does_not_add_up_is_reported_at_the_exit(self) -> None:
        deal = parse_deal(
            deal_with(
                exit={
                    "equity": [
                        {"name": "A", "of": "sponsor", "ownership": 0.4},
                        {"name": "B", "of": "rollover", "ownership": 0.4},
                    ]
                }
            )
        )
        with pytest.raises(DealSpecError, match="fully owned"):
            deal.realise()

    def test_a_validation_failure_names_the_security(self) -> None:
        with pytest.raises(DealSpecError, match=r"exit.equity\[0\]"):
            parse_deal(
                deal_with(
                    exit={
                        "equity": [
                            {
                                "name": "X",
                                "of": "sponsor",
                                "ownership": 1,
                                "preferred_rate": 0.08,
                            }
                        ]
                    }
                )
            )


class TestTheExitCommand:
    def test_the_worked_example_reports_every_security(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["exit", EXAMPLE])
        out = capsys.readouterr().out
        assert code == 0
        assert "Sponsor preferred" in out
        assert "Sponsor common" in out
        assert "Management rollover" in out
        assert "Where the value came from" in out

    def test_the_preferred_earns_its_coupon_and_the_common_earns_the_rest(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["exit", EXAMPLE, "--json"])
        payload = json.loads(capsys.readouterr().out)
        rows = {s["name"]: s for s in payload["securities"]}
        assert rows["Sponsor preferred"]["irr"] == pytest.approx(0.08, abs=1e-6)
        assert rows["Sponsor common"]["irr"] > rows["Sponsor preferred"]["irr"]
        assert payload["totals"]["irr"] < rows["Sponsor common"]["irr"]

    def test_the_bridge_ties_in_the_json_as_well(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["exit", EXAMPLE, "--json"])
        payload = json.loads(capsys.readouterr().out)
        a = payload["attribution"]
        assert a["reconciles"] is True
        parts = [
            money(a["ebitda_growth"]),
            money(a["multiple_change"]),
            money(a["debt_paydown"]),
            money(a["costs"]),
        ]
        assert sum(parts, money(0)) == money(a["total"])

    def test_the_waterfall_distributes_the_whole_equity_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["exit", EXAMPLE, "--json"])
        payload = json.loads(capsys.readouterr().out)
        paid = sum(
            (money(s["proceeds"]) for s in payload["securities"]), money(0)
        )
        assert paid == money(payload["exit"]["equity_value"])
        assert paid == money(payload["totals"]["proceeds"])

    def test_a_wipeout_is_reported_rather_than_crashed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "wipeout.json"
        path.write_text(json.dumps(deal_with(exit={"multiple": 1})), encoding="utf-8")
        code = main(["exit", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "wiped" in out
        assert "falls on the lenders" in out
        assert "n/a" in out  # no rate of return to report

    def test_an_unpaid_preferred_claim_is_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = deal_with(
            exit={
                "multiple": 3,
                "equity": [
                    {
                        "name": "Preferred",
                        "kind": "preferred",
                        "of": "sponsor",
                        "preferred_rate": 0.08,
                    },
                    {"name": "Rollover", "of": "rollover", "ownership": 1},
                ],
            }
        )
        path = tmp_path / "short.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        code = main(["exit", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "unpaid preferred claim" in out
