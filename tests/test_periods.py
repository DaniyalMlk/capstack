from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount
from capstack.periods import Frequency, Period, PeriodGrid, add_months, end_of_month


class TestAddMonths:
    def test_ordinary_advance(self) -> None:
        assert add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)

    def test_advance_across_a_year(self) -> None:
        assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)

    def test_thirty_first_january_clamps_to_end_of_february(self) -> None:
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_clamping_respects_a_leap_year(self) -> None:
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)

    def test_clamping_does_not_ratchet_down(self) -> None:
        # The bug this rule exists to prevent: measuring each step from the
        # previous result would give 31 Jan, 28 Feb, 28 Mar, 28 Apr.
        anchor = date(2026, 1, 31)
        assert [add_months(anchor, n) for n in range(4)] == [
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
            date(2026, 4, 30),
        ]

    def test_zero_months_is_the_identity(self) -> None:
        assert add_months(date(2026, 5, 4), 0) == date(2026, 5, 4)

    def test_negative_months_go_backwards(self) -> None:
        assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)

    def test_twelve_months_lands_on_the_same_day(self) -> None:
        assert add_months(date(2026, 7, 9), 12) == date(2027, 7, 9)

    def test_end_of_month_lookup(self) -> None:
        assert end_of_month(2026, 2) == 28
        assert end_of_month(2024, 2) == 29
        assert end_of_month(2026, 12) == 31


class TestFrequency:
    @pytest.mark.parametrize(
        ("frequency", "months", "per_year"),
        [
            (Frequency.ANNUAL, 12, 1),
            (Frequency.SEMI_ANNUAL, 6, 2),
            (Frequency.QUARTERLY, 3, 4),
            (Frequency.MONTHLY, 1, 12),
        ],
    )
    def test_step_sizes(self, frequency: Frequency, months: int, per_year: int) -> None:
        assert frequency.months == months
        assert frequency.periods_per_year == per_year


class TestPeriod:
    def test_days_and_year_fraction(self) -> None:
        p = Period(index=1, start=date(2026, 1, 1), end=date(2027, 1, 1))
        assert p.days == 365
        assert p.year_fraction(DayCount.ACT_365F) == 1

    def test_end_before_start_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ends"):
            Period(index=1, start=date(2026, 6, 1), end=date(2026, 5, 1))

    def test_negative_index_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            Period(index=-1, start=date(2026, 1, 1), end=date(2026, 2, 1))

    def test_label(self) -> None:
        p = Period(index=3, start=date(2028, 1, 1), end=date(2029, 1, 1))
        assert p.label == "P3 to 2029-01-01"


class TestPeriodGrid:
    def test_annual_grid_length_and_bounds(self) -> None:
        grid = PeriodGrid.build(date(2026, 6, 30), years=5, frequency=Frequency.ANNUAL)
        assert len(grid) == 5
        assert grid.start == date(2026, 6, 30)
        assert grid.end == date(2031, 6, 30)

    def test_quarterly_grid_has_four_periods_a_year(self) -> None:
        grid = PeriodGrid.build(date(2026, 3, 31), years=2, frequency=Frequency.QUARTERLY)
        assert len(grid) == 8
        assert grid.end == date(2028, 3, 31)

    def test_monthly_grid(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 31), years=1, frequency=Frequency.MONTHLY)
        assert len(grid) == 12
        # Month ends, correctly clamped, not drifting.
        assert grid[1].end == date(2026, 3, 31)
        assert grid[11].end == date(2027, 1, 31)

    def test_periods_are_contiguous(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 31), years=3, frequency=Frequency.QUARTERLY)
        for earlier, later in zip(grid.periods, grid.periods[1:]):
            assert later.start == earlier.end

    def test_indices_are_one_based_and_consecutive(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=5)
        assert [p.index for p in grid] == [1, 2, 3, 4, 5]

    def test_a_gap_in_the_grid_is_rejected(self) -> None:
        broken = (
            Period(index=1, start=date(2026, 1, 1), end=date(2027, 1, 1)),
            Period(index=2, start=date(2027, 2, 1), end=date(2028, 1, 1)),
        )
        with pytest.raises(ValueError, match="gap in grid"):
            PeriodGrid(periods=broken, frequency=Frequency.ANNUAL)

    def test_non_consecutive_indices_are_rejected(self) -> None:
        broken = (
            Period(index=1, start=date(2026, 1, 1), end=date(2027, 1, 1)),
            Period(index=5, start=date(2027, 1, 1), end=date(2028, 1, 1)),
        )
        with pytest.raises(ValueError, match="consecutively"):
            PeriodGrid(periods=broken, frequency=Frequency.ANNUAL)

    def test_empty_grid_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            PeriodGrid(periods=(), frequency=Frequency.ANNUAL)

    def test_zero_year_projection_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one year"):
            PeriodGrid.build(date(2026, 1, 1), years=0)

    def test_periods_tile_the_horizon_without_losing_a_day(self) -> None:
        # Twenty quarters must account for every elapsed day exactly once. A
        # single dropped day is a dropped day of interest accrual.
        grid = PeriodGrid.build(date(2026, 1, 1), years=5, frequency=Frequency.QUARTERLY)
        elapsed = (date(2031, 1, 1) - date(2026, 1, 1)).days
        assert sum(p.days for p in grid) == elapsed

    def test_year_fractions_sum_to_the_horizon(self) -> None:
        # Each quotient is rounded to working precision, so twenty of them sum
        # to the single-division answer only up to that precision, not exactly.
        grid = PeriodGrid.build(date(2026, 1, 1), years=5, frequency=Frequency.QUARTERLY)
        total = sum(p.year_fraction(DayCount.ACT_365F) for p in grid)
        elapsed = Decimal((date(2031, 1, 1) - date(2026, 1, 1)).days)
        assert abs(total - elapsed / Decimal(365)) < Decimal("1e-30")

    def test_locating_the_exit_period(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=5)
        assert grid.period_ending_on_or_after(date(2029, 6, 1)).index == 4

    def test_exit_beyond_the_horizon_is_rejected(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=5)
        with pytest.raises(ValueError, match="beyond the end"):
            grid.period_ending_on_or_after(date(2040, 1, 1))

    def test_indexing_and_iteration_agree(self) -> None:
        grid = PeriodGrid.build(date(2026, 1, 1), years=3)
        assert list(grid) == [grid[0], grid[1], grid[2]]
