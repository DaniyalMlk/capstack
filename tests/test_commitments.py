"""A facility committed at close and drawn later.

Two instruments live here and the tests exist mostly to keep them apart. A
revolver's capacity comes back when the balance is repaid, because that is what
revolving means. A term commitment — a delayed-draw facility, a committed
acquisition line — is consumed by the drawing and never returns, and it lapses at
the end of its availability period whether or not anyone took it down.

Describing the second as a term loan already drawn, which is what the engine used
to require, is wrong three ways: interest on money the business does not have,
leverage overstated from close, and the ticking fee — the actual cost of the
arrangement — missing entirely.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from capstack.daycount import DayCount
from capstack.debt import (
    AmortisationBasis,
    CapitalStructure,
    DebtSchedule,
    Tranche,
    TrancheKind,
)
from capstack.drivers import Driver
from capstack.events import AddOn, AddOnError, Draw
from capstack.money import ZERO, Numeric, is_close, money
from capstack.periods import Frequency, PeriodGrid

CLOSE = date(2026, 1, 1)
YEARS = 5
GRID = PeriodGrid.build(CLOSE, YEARS, Frequency.ANNUAL)
PERIODS = list(GRID)
CASH_FLOWS = ["120", "130", "140", "150", "160"]


def facility(**kwargs: object) -> Tranche:
    """A delayed-draw term loan: nothing drawn at close, 200 committed."""
    defaults: dict[str, object] = {
        "cash_rate": "0.05",
        "floating": False,
        "commitment": 200,
        "availability": 2,
        "undrawn_fee": "0.0175",
        "swept": False,
    }
    defaults.update(kwargs)
    face = defaults.pop("face", 0)
    return Tranche.of("Delayed draw", TrancheKind.TERM_LOAN, face, **defaults)  # type: ignore[arg-type]


def structure(*extra: Tranche, **kwargs: object) -> CapitalStructure:
    return CapitalStructure.of(
        [
            Tranche.of(
                "Term Loan B",
                TrancheKind.TERM_LOAN,
                600,
                cash_rate="0.06",
                floating=False,
                swept=False,
            ),
            *extra,
        ],
        day_count=DayCount.ACT_360,
        **kwargs,  # type: ignore[arg-type]
    )


def run(*extra: Tranche, acquisitions: list[AddOn] | None = None) -> DebtSchedule:
    return DebtSchedule.run(
        structure(*extra),
        PERIODS,
        CASH_FLOWS,
        opening_cash=300,
        acquisitions=acquisitions or [],
    )


class TestDescribingOne:
    def test_a_term_commitment_says_how_long_it_can_be_drawn(self) -> None:
        with pytest.raises(ValueError, match="how long it can be drawn"):
            Tranche.of("DDTL", TrancheKind.TERM_LOAN, 0, commitment=200)

    def test_a_revolver_needs_no_availability_period(self) -> None:
        """It is available until it matures, which it already knows."""
        revolver = Tranche.of("RCF", TrancheKind.REVOLVER, 0, commitment=150)
        assert revolver.has_commitment
        assert revolver.availability is None
        assert revolver.available_at(9)

    def test_availability_cannot_outlast_maturity(self) -> None:
        with pytest.raises(ValueError, match="after it matures"):
            Tranche.of(
                "DDTL",
                TrancheKind.TERM_LOAN,
                0,
                commitment=200,
                availability=6,
                maturity=5,
            )

    def test_availability_is_a_period_number(self) -> None:
        with pytest.raises(ValueError, match="1 or later"):
            Tranche.of(
                "DDTL", TrancheKind.TERM_LOAN, 0, commitment=200, availability=0
            )

    def test_drawn_at_close_cannot_exceed_the_commitment(self) -> None:
        with pytest.raises(ValueError, match="exceeds the commitment"):
            Tranche.of(
                "DDTL", TrancheKind.TERM_LOAN, 250, commitment=200, availability=2
            )

    def test_a_fee_needs_a_commitment_to_be_charged_on(self) -> None:
        with pytest.raises(ValueError, match="needs a commitment"):
            Tranche.of(
                "Notes", TrancheKind.NOTES, 100, undrawn_fee="0.005"
            )

    def test_a_partly_drawn_facility_is_a_legal_thing_to_describe(self) -> None:
        partial = facility(face=50)
        assert partial.face == money(50)
        assert partial.undrawn_at(money(50), period=1) == money(150)


class TestWhatCountsAsDrawn:
    """The one difference between the two instruments, stated directly."""

    def test_a_revolver_measures_against_the_balance(self) -> None:
        revolver = Tranche.of("RCF", TrancheKind.REVOLVER, 0, commitment=150)
        assert revolver.undrawn_at(money(60), period=1) == money(90)
        # Repaid, and the capacity is back.
        assert revolver.undrawn_at(ZERO, period=1) == money(150)

    def test_a_term_commitment_measures_against_what_was_taken_down(self) -> None:
        drawn = facility()
        assert drawn.undrawn_at(money(120), period=1) == money(80)
        # The caller passes cumulative face rather than the balance, so a
        # facility drawn and then repaid still has 80 left rather than 200.
        assert drawn.undrawn_at(money(120), period=2) == money(80)

    def test_a_lapsed_commitment_has_nothing_left(self) -> None:
        assert facility().undrawn_at(ZERO, period=3) == ZERO

    def test_a_matured_facility_has_nothing_left(self) -> None:
        lapsed = facility(availability=2, maturity=4)
        assert lapsed.undrawn_at(ZERO, period=4) == ZERO

    def test_a_tranche_with_no_commitment_has_no_capacity(self) -> None:
        plain = Tranche.of("Notes", TrancheKind.NOTES, 300, cash_rate="0.07")
        assert not plain.has_commitment
        assert plain.undrawn_at(ZERO, period=1) == ZERO


class TestTheTickingFee:
    def rows(self, **kwargs: object) -> DebtSchedule:
        return run(facility(**kwargs))

    def test_it_is_charged_on_capacity_nobody_has_drawn(self) -> None:
        first = self.rows()[0]
        # A whole year at ACT/360 on the full 200 at 175bp.
        expected = money(200) * money("0.0175") * PERIODS[0].year_fraction(
            DayCount.ACT_360
        )
        assert is_close(first.undrawn_fees, expected, tolerance="1E-20")

    def test_it_stops_when_the_availability_period_ends(self) -> None:
        charged = [row.undrawn_fees for row in self.rows()]
        assert all(fee > 0 for fee in charged[:2])
        assert all(fee == ZERO for fee in charged[2:])

    def test_it_is_the_only_thing_an_undrawn_facility_costs(self) -> None:
        """No interest, because there is no balance to charge it on."""
        first = self.rows()[0]
        assert first.tranche("Delayed draw").cash_interest == ZERO
        assert first.tranche("Delayed draw").closing == ZERO

    def test_an_undrawn_facility_adds_nothing_to_leverage(self) -> None:
        with_facility = self.rows()[0].closing_debt
        without = run()[0].closing_debt
        assert with_facility == without

    def test_a_facility_with_no_fee_costs_nothing_at_all(self) -> None:
        free = run(facility(undrawn_fee=0))[0]
        assert free.undrawn_fees == ZERO


class TestDrawingOnOne:
    def purchase(self, amount: Numeric, period: int = 1) -> AddOn:
        return AddOn.of(
            period,
            ebitda=20,
            multiple=8,
            draws=[Draw.of("Delayed draw", amount)],
            label="Bolt-on",
        )

    def test_a_draw_lands_on_the_facility(self) -> None:
        rows = run(facility(), acquisitions=[self.purchase(150)])
        assert rows[0].tranche("Delayed draw").closing == money(150)
        assert rows[1].tranche("Delayed draw").opening == money(150)

    def test_more_than_the_commitment_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="committed at 200"):
            run(facility(), acquisitions=[self.purchase(250)])

    def test_two_draws_that_overdraw_it_between_them_are_refused(self) -> None:
        """Each fits; the pair does not. Capacity is consumed in date order."""
        with pytest.raises(AddOnError, match="has 60 of it left"):
            run(
                facility(),
                acquisitions=[self.purchase(140, 1), self.purchase(120, 2)],
            )

    def test_two_draws_that_fit_are_allowed(self) -> None:
        rows = run(
            facility(),
            acquisitions=[self.purchase(140, 1), self.purchase(60, 2)],
        )
        assert rows[1].tranche("Delayed draw").closing == money(200)

    def test_a_draw_after_availability_ends_is_refused(self) -> None:
        with pytest.raises(AddOnError, match="available to draw until period 2"):
            run(facility(), acquisitions=[self.purchase(50, 3)])

    def test_an_uncommitted_facility_is_still_drawn_without_a_limit(self) -> None:
        """An accordion is a statement about a lender, not a checkable capacity."""
        accordion = Tranche.of(
            "Delayed draw",
            TrancheKind.TERM_LOAN,
            0,
            cash_rate="0.05",
            floating=False,
            swept=False,
        )
        rows = run(accordion, acquisitions=[self.purchase(400)])
        assert rows[0].tranche("Delayed draw").closing == money(400)

    def test_the_fee_falls_as_the_facility_is_drawn(self) -> None:
        rows = run(facility(), acquisitions=[self.purchase(150, 1)])
        undrawn_first, undrawn_second = rows[0].undrawn_fees, rows[1].undrawn_fees
        # The first year ticks on the whole 200; the second on the 50 left.
        assert undrawn_second < undrawn_first
        assert is_close(
            undrawn_second,
            money(50) * money("0.0175") * PERIODS[1].year_fraction(DayCount.ACT_360),
            tolerance="1E-20",
        )

    def test_a_drawn_facility_carries_interest_from_the_period_after(self) -> None:
        rows = run(facility(), acquisitions=[self.purchase(150, 1)])
        assert rows[0].tranche("Delayed draw").cash_interest == ZERO
        assert rows[1].tranche("Delayed draw").cash_interest > 0

    def test_a_draw_moves_the_amortisation_basis(self) -> None:
        """An instalment is a fraction of face, and the face is now larger."""
        amortising = facility(amortisation=Driver.constant("0.10", YEARS))
        rows = run(amortising, acquisitions=[self.purchase(150, 1)])
        assert rows[0].tranche("Delayed draw").mandatory_repayment == ZERO
        assert is_close(
            rows[1].tranche("Delayed draw").mandatory_repayment,
            money(15),
            tolerance="1E-20",
        )


class TestRepaymentDoesNotRestoreIt:
    """The distinction the whole change exists to draw."""

    def test_the_basis_remembers_what_was_taken_down(self) -> None:
        drawn = facility(amortisation=Driver.constant("0.50", YEARS))
        rows = run(
            drawn,
            acquisitions=[
                AddOn.of(
                    1,
                    ebitda=20,
                    multiple=8,
                    draws=[Draw.of("Delayed draw", 200)],
                    label="Bolt-on",
                )
            ],
        )
        # Half the facility is repaid in the period after it is drawn, so the
        # balance falls — and the fee stays at nothing, because the commitment
        # was consumed by the drawing rather than by the balance.
        assert rows[1].tranche("Delayed draw").closing < money(200)
        assert rows[1].undrawn_fees == ZERO

    def test_a_retired_facility_stops_ticking(self) -> None:
        basis = AmortisationBasis(structure(facility()))
        assert "Delayed draw" not in basis.retired
        basis.retire("Delayed draw")
        assert basis.retired == frozenset({"Delayed draw"})
        assert basis.at("Delayed draw") == ZERO


class TestTheShippedExample:
    """The buy-and-build file carries a real committed facility."""

    def deal(self) -> Any:
        root = Path(__file__).resolve().parent.parent
        with (root / "examples" / "thornbury.json").open() as handle:
            return json.load(handle)

    def facility(self) -> Any:
        return next(
            t for t in self.deal()["debt"] if t["name"] == "Acquisition facility"
        )

    def test_the_acquisition_facility_is_committed_and_ticks(self) -> None:
        line = self.facility()
        assert line["face"] == 0.0
        assert line["commitment"] == 150.0
        assert line["availability"] == 3
        assert line["undrawn_fee"] > 0

    def test_the_three_add_ons_fit_inside_it(self) -> None:
        drawn = sum(
            draw["amount"]
            for event in self.deal()["acquisitions"]
            for draw in event["draws"]
            if draw["tranche"] == "Acquisition facility"
        )
        assert drawn == 133.0
        assert drawn <= self.facility()["commitment"]

    def test_every_draw_falls_inside_the_availability_period(self) -> None:
        drawing = [
            event["period"]
            for event in self.deal()["acquisitions"]
            if any(d["tranche"] == "Acquisition facility" for d in event["draws"])
        ]
        assert max(drawing) <= self.facility()["availability"]
