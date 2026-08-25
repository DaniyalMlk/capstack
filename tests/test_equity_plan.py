"""The equity as a plan rather than as a set of amounts.

The point of the plan is that it survives a change of price. These tests hold a
file fixed, move the transaction under it, and check that the capital follows.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from capstack.money import money
from capstack.outcome import SecurityKind
from capstack.spec import DealSpecError, EquityPlan, Funding, SecurityPlan, parse_deal
from capstack.transaction import EntryValuation, Transaction


def transaction(multiple: str = "8", rollover: str = "100") -> Transaction:
    return Transaction.of(
        EntryValuation.of(200, multiple, existing_debt=300, existing_cash=50),
        debt=(),
        rollover_equity=rollover,
    )


class TestResolvingCapital:
    def test_a_share_of_the_sponsor_cheque_follows_the_cheque(self) -> None:
        plan = SecurityPlan(
            name="Sponsor common",
            kind=SecurityKind.COMMON,
            funding=Funding.SPONSOR,
            share=money("0.6"),
            ownership=money(1),
        )
        cheap, dear = transaction("8"), transaction("9")
        assert dear.sponsor_equity > cheap.sponsor_equity
        assert plan.capital(cheap) == cheap.sponsor_equity * money("0.6")
        assert plan.capital(dear) == dear.sponsor_equity * money("0.6")

    def test_a_stated_amount_does_not_move(self) -> None:
        plan = SecurityPlan(
            name="Co-investor",
            kind=SecurityKind.COMMON,
            funding=Funding.STATED,
            amount=money(75),
            ownership=money(1),
        )
        assert plan.capital(transaction("8")) == money(75)
        assert plan.capital(transaction("14")) == money(75)

    def test_a_rollover_share_follows_the_rollover(self) -> None:
        plan = SecurityPlan(
            name="Management",
            kind=SecurityKind.COMMON,
            funding=Funding.ROLLOVER,
            ownership=money(1),
        )
        assert plan.capital(transaction(rollover="140")) == money(140)

    def test_a_negative_plug_contributes_nothing_rather_than_less_than_nothing(
        self,
    ) -> None:
        # More debt raised than the purchase needs: the plug is negative, which
        # is cash off the table and not capital invested at a negative multiple.
        over_funded = Transaction.of(
            EntryValuation.of(200, 6),
            debt=(),
            rollover_equity=5000,
        )
        assert over_funded.sponsor_equity < 0
        plan = SecurityPlan(
            name="Sponsor",
            kind=SecurityKind.COMMON,
            funding=Funding.SPONSOR,
            ownership=money(1),
        )
        assert plan.capital(over_funded) == 0


class TestPlanValidation:
    def test_shares_of_one_cheque_must_come_to_one(self) -> None:
        with pytest.raises(ValueError, match="allocated"):
            EquityPlan(
                plans=(
                    SecurityPlan(
                        name="A",
                        kind=SecurityKind.COMMON,
                        funding=Funding.SPONSOR,
                        share=money("0.5"),
                        ownership=money(1),
                    ),
                )
            )

    def test_two_cheques_are_allocated_independently(self) -> None:
        plan = EquityPlan(
            plans=(
                SecurityPlan(
                    name="Sponsor",
                    kind=SecurityKind.COMMON,
                    funding=Funding.SPONSOR,
                    ownership=money("0.8"),
                ),
                SecurityPlan(
                    name="Rollover",
                    kind=SecurityKind.COMMON,
                    funding=Funding.ROLLOVER,
                    ownership=money("0.2"),
                ),
            )
        )
        assert len(plan) == 2
        assert bool(plan)
        assert [p.name for p in plan] == ["Sponsor", "Rollover"]

    def test_a_stated_amount_is_not_allocated_against_a_cheque(self) -> None:
        # A co-investor alongside a fully allocated sponsor cheque is legitimate
        # and must not be counted into anyone else's allocation.
        plan = EquityPlan(
            plans=(
                SecurityPlan(
                    name="Sponsor",
                    kind=SecurityKind.COMMON,
                    funding=Funding.SPONSOR,
                    ownership=money("0.7"),
                ),
                SecurityPlan(
                    name="Co-investor",
                    kind=SecurityKind.COMMON,
                    funding=Funding.STATED,
                    amount=money(50),
                    ownership=money("0.3"),
                ),
            )
        )
        assert len(plan.resolve(transaction())) == 2

    def test_names_must_be_distinct(self) -> None:
        twin = SecurityPlan(
            name="Same",
            kind=SecurityKind.COMMON,
            funding=Funding.STATED,
            amount=money(10),
            ownership=money("0.5"),
        )
        with pytest.raises(ValueError, match="distinct"):
            EquityPlan(plans=(twin, twin))

    def test_a_preferred_return_needs_preferred_capital(self) -> None:
        with pytest.raises(ValueError, match="preferred capital"):
            SecurityPlan(
                name="Common",
                kind=SecurityKind.COMMON,
                funding=Funding.SPONSOR,
                preferred_rate=money("0.08"),
            )

    @pytest.mark.parametrize(
        "field,value,message",
        [
            ("share", money("1.5"), "between 0 and 1"),
            ("ownership", money("-0.1"), "between 0 and 1"),
            ("amount", money(-5), "not be negative"),
            ("seniority", -1, "not be negative"),
            ("name", "  ", "needs a name"),
        ],
    )
    def test_structural_errors_are_caught_without_a_transaction(
        self, field: str, value: object, message: str
    ) -> None:
        base = SecurityPlan(
            name="Sponsor",
            kind=SecurityKind.COMMON,
            funding=Funding.SPONSOR,
            ownership=money(1),
        )
        change: dict[str, Any] = {field: value}
        with pytest.raises(ValueError, match=message):
            dataclasses.replace(base, **change)


class TestReadingAPlanFromAFile:
    def payload(self, equity: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "name": "Test",
            "close_date": "2026-06-30",
            "entry": {"ltm_ebitda": 200, "multiple": 8},
            "debt": [
                {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05}
            ],
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
            "exit": {"multiple": 9, "equity": equity},
        }

    def test_the_plan_is_kept_and_the_securities_are_derived(self) -> None:
        deal = parse_deal(
            self.payload(
                [
                    {
                        "name": "Sponsor preferred",
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
                    {"name": "Rollover", "of": "rollover", "ownership": 0.25},
                ]
            )
        )
        assert [p.funding for p in deal.equity] == [
            Funding.SPONSOR,
            Funding.SPONSOR,
            Funding.ROLLOVER,
        ]
        sponsor = deal.transaction.sponsor_equity
        preferred, common, rollover = deal.securities
        assert preferred.invested == sponsor * money("0.9")
        assert common.invested == sponsor * money("0.1")
        assert rollover.invested == money(100)

    def test_the_derived_stack_moves_with_the_price(self) -> None:
        deal = parse_deal(
            self.payload([{"name": "Sponsor", "of": "sponsor", "ownership": 1}])
        )
        before = deal.securities[0].invested
        dearer = dataclasses.replace(
            deal,
            transaction=dataclasses.replace(
                deal.transaction,
                valuation=dataclasses.replace(
                    deal.transaction.valuation, entry_multiple=money(10)
                ),
            ),
        )
        after = dearer.securities[0].invested
        assert after > before
        assert after == dearer.transaction.sponsor_equity

    def test_an_underallocated_cheque_is_rejected_when_the_file_is_read(self) -> None:
        with pytest.raises(DealSpecError, match="allocated"):
            parse_deal(
                self.payload(
                    [{"name": "Sponsor", "of": "sponsor", "share": 0.5, "ownership": 1}]
                )
            )

    def test_an_unknown_cheque_is_named_in_the_error(self) -> None:
        with pytest.raises(DealSpecError, match="unknown source"):
            parse_deal(self.payload([{"name": "X", "of": "lender", "ownership": 1}]))

    def test_a_share_out_of_range_is_caught_at_the_file_level(self) -> None:
        with pytest.raises(DealSpecError, match="between 0 and 1"):
            parse_deal(
                self.payload(
                    [{"name": "Sponsor", "of": "sponsor", "share": 1.4, "ownership": 1}]
                )
            )
