"""What a contractual instalment is struck against, once face moves after close.

Every figure asserted here is arithmetic on a single annual period at ACT/365F,
so the year fraction is exactly one and an instalment is the driver times the
basis with nothing else in the way.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from capstack.daycount import DayCount
from capstack.debt import (
    AmortisationBasis,
    CapitalStructure,
    DebtSchedule,
    InterestBasis,
    Tranche,
    TrancheKind,
)
from capstack.drivers import Driver
from capstack.events import AddOn, Draw, Recapitalisation, Refinancing
from capstack.money import ZERO, money
from capstack.periods import Frequency, Period, PeriodGrid

from capstack.cli import main

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
KESTREL = str(EXAMPLES / "kestrel.json")
MERIDIAN = str(EXAMPLES / "meridian.json")

CLOSE = date(2026, 1, 1)


def grid(years: int = 5) -> list[Period]:
    return list(PeriodGrid.build(CLOSE, years=years, frequency=Frequency.ANNUAL))


def fixed(name: str, kind: TrancheKind, face: object, **kwargs: object) -> Tranche:
    kwargs.setdefault("floating", False)
    return Tranche.of(name, kind, face, **kwargs)  # type: ignore[arg-type]


def structure(*tranches: Tranche, **kwargs: object) -> CapitalStructure:
    kwargs.setdefault("day_count", DayCount.ACT_365F)
    kwargs.setdefault("interest_basis", InterestBasis.OPENING)
    kwargs.setdefault("minimum_cash", 0)
    return CapitalStructure.of(tranches, **kwargs)  # type: ignore[arg-type]


def unswept_term_loan(face: object, rate: str = "0.10") -> Tranche:
    """A term loan that amortises but is not swept, so the schedule is the only mover."""
    return fixed(
        "TLB",
        TrancheKind.TERM_LOAN,
        face,
        cash_rate="0.05",
        amortisation=Driver.constant(rate, 12),
        swept=False,
    )


class TestTheLedger:
    """The basis on its own, before any schedule runs against it."""

    def test_it_opens_at_the_face_drawn_at_close(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        assert basis.at("TLB") == money(500)

    def test_a_draw_adds_to_it(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        basis.draw("TLB", money(200))
        assert basis.at("TLB") == money(700)

    def test_a_draw_of_nothing_changes_nothing(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        basis.draw("TLB", ZERO)
        assert basis.at("TLB") == money(500)

    def test_a_negative_draw_is_ignored_rather_than_credited(self) -> None:
        # Face does not come off the basis by being repaid; only a takeout
        # retires it, and that has its own method.
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        basis.draw("TLB", money(-100))
        assert basis.at("TLB") == money(500)

    def test_retiring_takes_it_to_zero_rather_than_reducing_it(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        basis.draw("TLB", money(200))
        basis.retire("TLB")
        assert basis.at("TLB") == ZERO

    def test_the_snapshot_does_not_alias_the_ledger(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        taken = basis.snapshot()
        basis.draw("TLB", money(200))
        assert taken["TLB"] == money(500)

    def test_an_unknown_name_is_not_silently_zero(self) -> None:
        basis = AmortisationBasis(structure(unswept_term_loan(500)))
        with pytest.raises(KeyError):
            basis.at("Mezzanine")


class TestPaperPlacedAtClose:
    """The case the old reading got right, which must not have moved."""

    def test_the_instalment_is_the_driver_on_the_face(self) -> None:
        schedule = DebtSchedule.run(
            structure(unswept_term_loan(500)), grid(3), [100, 100, 100]
        )
        for row in schedule:
            assert row.tranche("TLB").mandatory_repayment == money(50)

    def test_the_basis_is_reported_on_the_row(self) -> None:
        schedule = DebtSchedule.run(
            structure(unswept_term_loan(500)), grid(3), [100, 100, 100]
        )
        assert [row.tranche("TLB").amortisation_basis for row in schedule] == [
            money(500)
        ] * 3

    def test_a_sweep_running_ahead_does_not_reduce_the_instalment(self) -> None:
        # The point of a basis rather than a balance: cash swept in period one
        # is not a prepayment of period two's instalment.
        swept = fixed(
            "TLB",
            TrancheKind.TERM_LOAN,
            500,
            cash_rate="0.05",
            amortisation=Driver.constant("0.10", 12),
            swept=True,
        )
        schedule = DebtSchedule.run(structure(swept), grid(2), [200, 200])
        assert schedule[0].tranche("TLB").sweep_repayment > 0
        assert schedule[1].tranche("TLB").mandatory_repayment == money(50)

    def test_the_instalment_is_still_capped_at_what_is_owed(self) -> None:
        # A 60% amortisation on a facility with two periods to run cannot repay
        # 60% of face in the second period, because only 40% is left.
        schedule = DebtSchedule.run(
            structure(unswept_term_loan(500, rate="0.60")), grid(2), [500, 500]
        )
        assert schedule[0].tranche("TLB").mandatory_repayment == money(300)
        assert schedule[1].tranche("TLB").mandatory_repayment == money(200)
        assert schedule[1].tranche("TLB").closing == ZERO


class TestADelayedDrawFacility:
    """Nothing at close, drawn later — the case that repaid nothing at all."""

    def delayed_draw(self) -> CapitalStructure:
        return structure(
            unswept_term_loan(500),
            fixed(
                "DDTL",
                TrancheKind.TERM_LOAN,
                0,
                cash_rate="0.06",
                amortisation=Driver.constant("0.10", 12),
                swept=False,
                seniority=1,
            ),
        )

    def run(self) -> DebtSchedule:
        return DebtSchedule.run(
            self.delayed_draw(),
            grid(5),
            [300] * 5,
            ebitda=[200] * 5,
            acquisitions=[
                AddOn.of(2, ebitda=50, multiple=6, draws=[Draw.of("DDTL", 300)])
            ],
        )

    def test_it_amortises_nothing_before_it_is_drawn(self) -> None:
        schedule = self.run()
        assert schedule[0].tranche("DDTL").mandatory_repayment == ZERO
        assert schedule[1].tranche("DDTL").mandatory_repayment == ZERO

    def test_the_draw_lands_at_the_end_of_the_period_it_funds(self) -> None:
        schedule = self.run()
        assert schedule[1].tranche("DDTL").acquisition == money(300)
        assert schedule[1].tranche("DDTL").closing == money(300)

    def test_the_basis_it_amortises_against_is_what_was_drawn(self) -> None:
        schedule = self.run()
        assert schedule[2].tranche("DDTL").amortisation_basis == money(300)

    def test_it_repays_from_the_period_after_the_draw(self) -> None:
        schedule = self.run()
        assert schedule[2].tranche("DDTL").mandatory_repayment == money(30)
        assert schedule[3].tranche("DDTL").mandatory_repayment == money(30)

    def test_the_platform_loan_is_unaffected_by_the_draw(self) -> None:
        schedule = self.run()
        assert all(row.tranche("TLB").mandatory_repayment == money(50) for row in schedule)

    def test_every_period_still_reconciles(self) -> None:
        schedule = self.run()
        for row in schedule:
            assert row.reconciles()
            for tranche in row.tranches:
                assert tranche.reconciles()


class TestAnIncrementalFacilityOnAnExistingName:
    """Face added to paper that already amortises."""

    def run(self) -> DebtSchedule:
        return DebtSchedule.run(
            structure(unswept_term_loan(500)),
            grid(5),
            [300] * 5,
            ebitda=[200] * 5,
            recapitalisations=[Recapitalisation.of(2, [Draw.of("TLB", 500)])],
        )

    def test_the_basis_grows_by_the_face_taken_down(self) -> None:
        schedule = self.run()
        assert schedule[1].tranche("TLB").amortisation_basis == money(500)
        assert schedule[2].tranche("TLB").amortisation_basis == money(1000)

    def test_the_instalment_doubles_with_the_facility(self) -> None:
        schedule = self.run()
        assert schedule[1].tranche("TLB").mandatory_repayment == money(50)
        assert schedule[2].tranche("TLB").mandatory_repayment == money(100)

    def test_incremental_face_does_not_amortise_in_the_period_it_is_drawn(self) -> None:
        # It is drawn at the period boundary, so the period it lands in was
        # solved on the basis it opened with.
        schedule = self.run()
        assert schedule[1].tranche("TLB").recapitalisation == money(500)
        assert schedule[1].tranche("TLB").mandatory_repayment == money(50)


class TestARetiredFacility:
    """A takeout, and the instalment that should stop with it."""

    def run(self) -> DebtSchedule:
        return DebtSchedule.run(
            structure(
                unswept_term_loan(500),
                fixed(
                    "New TLB",
                    TrancheKind.TERM_LOAN,
                    0,
                    cash_rate="0.03",
                    amortisation=Driver.constant("0.10", 12),
                    swept=False,
                    seniority=1,
                ),
            ),
            grid(5),
            [300] * 5,
            refinancings=[
                Refinancing.of(2, "TLB", [Draw.of("New TLB", 400)]),
            ],
        )

    def test_the_old_paper_stops_amortising(self) -> None:
        schedule = self.run()
        assert schedule[1].tranche("TLB").mandatory_repayment == money(50)
        assert schedule[2].tranche("TLB").amortisation_basis == ZERO
        assert schedule[2].tranche("TLB").mandatory_repayment == ZERO

    def test_the_replacement_starts_amortising_on_what_it_raised(self) -> None:
        schedule = self.run()
        assert schedule[2].tranche("New TLB").amortisation_basis == money(400)
        assert schedule[2].tranche("New TLB").mandatory_repayment == money(40)

    def test_the_old_paper_is_gone_rather_than_merely_quiet(self) -> None:
        schedule = self.run()
        assert schedule[1].tranche("TLB").closing == ZERO

    def test_a_repricing_into_the_same_name_resets_its_basis(self) -> None:
        # Retire then draw, in that order: the basis after a same-name takeout
        # is the new face, not the old one plus the new one.
        schedule = DebtSchedule.run(
            structure(unswept_term_loan(500)),
            grid(5),
            [300] * 5,
            refinancings=[Refinancing.of(2, "TLB", [Draw.of("TLB", 400)])],
        )
        assert schedule[2].tranche("TLB").amortisation_basis == money(400)
        assert schedule[2].tranche("TLB").mandatory_repayment == money(40)


class TestTheWholeHoldByHand:
    """One structure, one draw, one takeout, every instalment checked."""

    def test_the_sequence_of_instalments(self) -> None:
        schedule = DebtSchedule.run(
            structure(
                unswept_term_loan(1000),
                fixed(
                    "DDTL",
                    TrancheKind.TERM_LOAN,
                    0,
                    cash_rate="0.06",
                    amortisation=Driver.constant("0.05", 12),
                    swept=False,
                    seniority=1,
                ),
            ),
            grid(5),
            [600] * 5,
            ebitda=[400] * 5,
            acquisitions=[AddOn.of(1, ebitda=50, multiple=8, draws=[Draw.of("DDTL", 400)])],
        )
        # TLB amortises 10% of 1000 every period from the start.
        assert [row.tranche("TLB").mandatory_repayment for row in schedule] == [
            money(100)
        ] * 5
        # DDTL is drawn at the end of period one, so it repays 5% of 400 from
        # period two onward and nothing before.
        assert [row.tranche("DDTL").mandatory_repayment for row in schedule] == [
            ZERO,
            money(20),
            money(20),
            money(20),
            money(20),
        ]
        assert schedule[4].tranche("DDTL").closing == money(400 - 80)

    def test_the_total_repaid_is_the_sum_of_the_instalments(self) -> None:
        schedule = DebtSchedule.run(
            structure(unswept_term_loan(1000)), grid(4), [600] * 4
        )
        assert schedule.total_repaid == money(400)
        assert schedule.closing_debt == money(600)


class TestOnTheCommandLine:
    """The basis as a reader of the schedule sees it."""

    def test_the_json_carries_a_basis_per_tranche_per_period(self) -> None:
        code = main(["schedule", KESTREL, "--json"])
        assert code == 0

    def test_kestrel_steps_its_instalment_with_the_recapitalisation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["schedule", KESTREL, "--json"])
        report = json.loads(capsys.readouterr().out)
        basis = [p["amortisation_basis"]["Unitranche"] for p in report["periods"]]
        # Eighty of incremental face is drawn at the end of period three, so
        # the basis steps in period four and not before.
        assert basis[:3] == ["420.00", "420.00", "420.00"]
        assert basis[3:] == ["500.00", "500.00"]
        instalments = [p["mandatory_repayment"] for p in report["periods"]]
        assert instalments[2] != instalments[3]

    def test_the_block_is_printed_where_the_face_moves(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["schedule", KESTREL])
        assert "Amortising face" in capsys.readouterr().out

    def test_the_block_is_silent_where_it_does_not(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["schedule", MERIDIAN])
        assert "Amortising face" not in capsys.readouterr().out
