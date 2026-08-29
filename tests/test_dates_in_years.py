"""A file that means the same thing on any frequency.

Assumptions are read by the year a period falls in, so an operating case already
describes the same years however often they are reported. Everything a file
states as a *period number* was still a column, and a column is a different date
on every frequency: `maturity: 7` retires a term loan in year seven on an annual
grid and in quarter seven on a quarterly one, a year and a half into a five-year
hold, with nothing raised.

The fix is not to reinterpret period numbers. A period number is the right unit
for something that genuinely happens in a particular quarter. It is to let a file
say which of the two it means.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from capstack.money import is_close, money
from capstack.periods import Frequency, PeriodGrid
from capstack.spec import DealSpecError, parse_deal

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def meridian(*, frequency: str = "annual", in_years: bool = True) -> dict[str, Any]:
    """The shipped five-year deal, on a chosen grid.

    The file states its maturities in years, which is the point of the change.
    ``in_years=False`` rewrites them as bare period numbers, which is what the
    file said before and what a file written against an annual grid still says.
    """
    with (EXAMPLES / "meridian.json").open() as handle:
        deal: dict[str, Any] = json.load(handle)
    deal["projection"] = {**deal["projection"], "frequency": frequency}
    if not in_years:
        for tranche in deal["debt"]:
            stated = tranche.get("maturity")
            if isinstance(stated, str) and stated.endswith("y"):
                tranche["maturity"] = int(stated[:-1])
    return deal


class TestTranslatingAYear:
    @pytest.mark.parametrize(
        ("frequency", "expected"),
        [
            (Frequency.ANNUAL, 7),
            (Frequency.SEMI_ANNUAL, 14),
            (Frequency.QUARTERLY, 28),
            (Frequency.MONTHLY, 84),
        ],
    )
    def test_the_end_of_a_year_is_a_period_on_every_grid(
        self, frequency: Frequency, expected: int
    ) -> None:
        assert frequency.period_ending_year(7) == expected

    def test_years_are_numbered_from_one(self) -> None:
        with pytest.raises(ValueError, match="numbered from one"):
            Frequency.QUARTERLY.period_ending_year(0)


class TestHowManyYearsAGridReads:
    @pytest.mark.parametrize("frequency", list(Frequency))
    def test_a_five_year_grid_reads_five_years(self, frequency: Frequency) -> None:
        assert PeriodGrid.build(date(2026, 1, 1), 5, frequency).years == 5

    def test_a_stub_is_not_a_year_of_its_own(self) -> None:
        stubbed = PeriodGrid.build(
            date(2026, 11, 15),
            5,
            Frequency.QUARTERLY,
            stub_to=date(2027, 1, 1),
        )
        assert stubbed.has_stub
        assert stubbed.years == 5

    def test_a_part_year_still_counts_as_one(self) -> None:
        """Six quarters is a year and a half, and reads two years of assumptions."""
        grid = PeriodGrid(
            periods=PeriodGrid.build(date(2026, 1, 1), 2, Frequency.QUARTERLY).periods[
                :6
            ],
            frequency=Frequency.QUARTERLY,
        )
        assert grid.years == 2


class TestStatingItInAFile:
    def test_a_maturity_in_years_lands_on_the_same_date(self) -> None:
        annual = parse_deal(meridian(frequency="annual"))
        quarterly = parse_deal(meridian(frequency="quarterly"))
        assert annual.structure is not None and quarterly.structure is not None
        for name in ("Term Loan B",):
            assert annual.structure.tranche(name).maturity == 7
            assert quarterly.structure.tranche(name).maturity == 28

    def test_a_bare_number_is_still_a_period(self) -> None:
        """Unchanged, and deliberately so: a column is the right unit for some things."""
        quarterly = parse_deal(meridian(frequency="quarterly", in_years=False))
        assert quarterly.structure is not None
        assert quarterly.structure.tranche("Term Loan B").maturity == 7

    def test_an_availability_period_reads_the_same_way(self) -> None:
        deal = parse_deal(
            {
                "entry": {"ltm_ebitda": 100, "multiple": 10},
                "close_date": "2026-01-01",
                "projection": {"years": 5, "frequency": "quarterly"},
                "operating": {
                    "opening_revenue": 500,
                    "revenue_growth": {"constant": 0.05},
                    "ebitda_margin": {"constant": 0.2},
                    "da_rate": {"constant": 0.03},
                    "capex_rate": {"constant": 0.04},
                    "nwc_rate": {"constant": 0.15},
                    "tax_rate": 0.25,
                },
                "debt": [
                    {
                        "name": "Acquisition facility",
                        "kind": "term_loan",
                        "face": 0,
                        "commitment": 200,
                        "availability": "2y",
                        "maturity": "5y",
                        "cash_rate": 0.05,
                        "floating": False,
                        "undrawn_fee": 0.0175,
                    }
                ],
                "structure": {},
            }
        )
        assert deal.structure is not None
        facility = deal.structure.tranche("Acquisition facility")
        assert facility.availability == 8
        assert facility.maturity == 20

    def test_a_covenant_can_start_testing_in_a_year(self) -> None:
        deal = meridian(frequency="quarterly")
        deal["covenants"][0]["first_test_period"] = "2y"
        parsed = parse_deal(deal)
        assert parsed.covenants[0].first_test_period == 8

    def test_a_year_that_is_not_a_number_is_refused(self) -> None:
        deal = meridian(frequency="quarterly")
        deal["debt"][1]["maturity"] = "latery"
        with pytest.raises(DealSpecError, match="not a number of years"):
            parse_deal(deal)

    def test_year_zero_is_refused(self) -> None:
        deal = meridian(frequency="quarterly")
        deal["debt"][1]["maturity"] = "0y"
        with pytest.raises(DealSpecError, match="numbered from one"):
            parse_deal(deal)


class TestAnAssumptionSeriesIsAsLongAsTheCase:
    """A ramp is written over years, so it is expanded over years.

    Expanding it over columns and then reading it by year is the failure this
    catches: on a quarterly grid a five-year taper became a twenty-step taper,
    of which only the first five were ever read, so the business tapered over a
    quarter of the span the file described. Nothing raised, and the exit EBITDA
    came out 2% light.
    """

    @pytest.mark.parametrize(
        "frequency", ["annual", "semi-annual", "quarterly", "monthly"]
    )
    def test_a_ramp_covers_the_same_years_on_every_grid(
        self, frequency: str
    ) -> None:
        deal = parse_deal(meridian(frequency=frequency))
        assert deal.operating is not None
        # The file ramps growth from 8.5% to 3.5% across the five-year case.
        growth = deal.operating.revenue_growth
        assert len(growth) == 5
        assert growth.at(0) == money("0.085")
        assert growth.at(4) == money("0.035")

    @pytest.mark.parametrize(
        "frequency", ["semi-annual", "quarterly", "monthly"]
    )
    def test_the_case_reaches_the_same_earnings(self, frequency: str) -> None:
        annual = parse_deal(meridian()).project()
        other = parse_deal(meridian(frequency=frequency)).project()
        assert is_close(other.exit_ebitda, annual.exit_ebitda, tolerance="1E-20")
        assert is_close(other.entry_ebitda, annual.entry_ebitda, tolerance="1E-20")


class TestTheWholeDealOnTwoFrequencies:
    """What the change is for: one file, two grids, one underwriting."""

    def outcome(self, frequency: str) -> Any:
        return parse_deal(meridian(frequency=frequency)).realise()

    @pytest.mark.parametrize("frequency", ["semi-annual", "quarterly"])
    def test_the_exit_is_priced_on_the_same_earnings(self, frequency: str) -> None:
        annual, other = self.outcome("annual"), self.outcome(frequency)
        assert is_close(
            other.valuation.ebitda, annual.valuation.ebitda, tolerance="1E-20"
        )
        assert other.valuation.multiple == annual.valuation.multiple

    @pytest.mark.parametrize("frequency", ["semi-annual", "quarterly"])
    def test_the_paper_is_still_outstanding_at_the_exit(
        self, frequency: str
    ) -> None:
        """The symptom: stated as a column, the term loan retired in year two."""
        annual, other = self.outcome("annual"), self.outcome(frequency)
        # Within a per cent of each other. They are not identical, and should not
        # be: a quarterly model accrues interest on shorter periods and sweeps
        # four times a year, which genuinely deleverages differently.
        assert other.valuation.debt > annual.valuation.debt * money("0.98")
        assert other.valuation.debt < annual.valuation.debt * money("1.02")

    @pytest.mark.parametrize("frequency", ["semi-annual", "quarterly"])
    def test_the_sponsor_makes_about_the_same_money(self, frequency: str) -> None:
        annual, other = self.outcome("annual"), self.outcome(frequency)
        assert other.valuation.equity_value > annual.valuation.equity_value * money("0.98")
        assert other.valuation.equity_value < annual.valuation.equity_value * money("1.02")
