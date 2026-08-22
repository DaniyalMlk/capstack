from datetime import date
from decimal import Decimal

import pytest

from capstack.daycount import DayCount, days_between, is_leap_year, year_fraction
from capstack.money import money


class TestLeapYear:
    def test_ordinary_leap_year(self) -> None:
        assert is_leap_year(2024)

    def test_century_is_not_a_leap_year(self) -> None:
        assert not is_leap_year(1900)
        assert not is_leap_year(2100)

    def test_four_hundredth_year_is_a_leap_year(self) -> None:
        assert is_leap_year(2000)

    def test_ordinary_year(self) -> None:
        assert not is_leap_year(2023)


class TestActualConventions:
    def test_days_between_is_signed(self) -> None:
        assert days_between(date(2026, 1, 1), date(2026, 1, 31)) == 30
        assert days_between(date(2026, 1, 31), date(2026, 1, 1)) == -30

    def test_act_365f_full_year(self) -> None:
        f = year_fraction(date(2026, 1, 1), date(2027, 1, 1), DayCount.ACT_365F)
        assert f == Decimal(365) / Decimal(365)

    def test_act_365f_does_not_adjust_for_leap_years(self) -> None:
        # Fixed 365 denominator, so a leap year is worth slightly more than one.
        f = year_fraction(date(2024, 1, 1), date(2025, 1, 1), DayCount.ACT_365F)
        assert f == Decimal(366) / Decimal(365)

    def test_act_360_quarter(self) -> None:
        # 90 actual days over a 360 denominator is exactly a quarter.
        f = year_fraction(date(2026, 1, 1), date(2026, 4, 1), DayCount.ACT_360)
        assert f == Decimal(90) / Decimal(360)

    def test_act_360_exceeds_one_over_a_calendar_year(self) -> None:
        f = year_fraction(date(2026, 1, 1), date(2027, 1, 1), DayCount.ACT_360)
        assert f == Decimal(365) / Decimal(360)

    def test_the_conventions_disagree_by_real_money(self) -> None:
        # 500bp on 500m accrued over a calendar year. ACT/360 charges 365 days
        # against a 360-day year, so it collects about 347k more. This gap is
        # the reason credit agreements name the convention.
        start, end = date(2026, 1, 1), date(2027, 1, 1)
        notional, margin = money(500_000_000), money("0.05")
        act360 = notional * margin * year_fraction(start, end, DayCount.ACT_360)
        act365 = notional * margin * year_fraction(start, end, DayCount.ACT_365F)
        assert act360 > act365
        assert Decimal(340_000) < act360 - act365 < Decimal(350_000)


class TestThirty360US:
    """Checked against the standard published 30/360 bond-basis examples."""

    @pytest.mark.parametrize(
        ("start", "end", "expected_days"),
        [
            (date(2007, 1, 15), date(2007, 1, 30), 15),
            (date(2007, 1, 15), date(2007, 2, 15), 30),
            (date(2007, 1, 15), date(2007, 7, 15), 180),
            (date(2007, 9, 30), date(2007, 10, 31), 30),
            (date(2007, 9, 30), date(2008, 3, 31), 180),
            (date(2007, 1, 31), date(2007, 2, 28), 28),
            (date(2006, 8, 31), date(2007, 2, 28), 178),
            (date(2007, 2, 28), date(2007, 3, 31), 33),
            (date(2007, 8, 31), date(2008, 2, 29), 179),
            (date(2008, 2, 29), date(2008, 8, 31), 182),
            (date(2007, 12, 31), date(2008, 1, 31), 30),
        ],
    )
    def test_published_examples(self, start: date, end: date, expected_days: int) -> None:
        f = year_fraction(start, end, DayCount.THIRTY_360_US)
        assert f == Decimal(expected_days) / Decimal(360)

    def test_end_day_31_is_not_pulled_back_when_start_day_is_early(self) -> None:
        # The asymmetry that implementations usually get wrong. 28 February to
        # 31 March genuinely earns 33 days, not 30.
        f = year_fraction(date(2007, 2, 28), date(2007, 3, 31), DayCount.THIRTY_360_US)
        assert f == Decimal(33) / Decimal(360)

    def test_a_thirty_360_year_is_exactly_one(self) -> None:
        f = year_fraction(date(2026, 3, 15), date(2027, 3, 15), DayCount.THIRTY_360_US)
        assert f == Decimal(1)


class TestActActISDA:
    """Checked against the worked examples published with the ISDA definitions."""

    def test_period_inside_a_single_non_leap_year(self) -> None:
        f = year_fraction(date(1999, 2, 1), date(1999, 7, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(150) / Decimal(365)
        assert round(float(f), 9) == 0.410958904

    def test_period_straddling_a_year_boundary_into_a_leap_year(self) -> None:
        # 61 days in 1999-style 365 basis plus 121 days on a 366 basis.
        f = year_fraction(date(2003, 11, 1), date(2004, 5, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(61) / Decimal(365) + Decimal(121) / Decimal(366)
        assert round(float(f), 8) == 0.49772438

    def test_period_straddling_two_non_leap_years(self) -> None:
        f = year_fraction(date(2002, 8, 15), date(2003, 7, 15), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(334) / Decimal(365)
        assert round(float(f), 9) == 0.915068493

    def test_short_period_across_new_year(self) -> None:
        f = year_fraction(date(2011, 12, 28), date(2012, 1, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(4) / Decimal(365)

    def test_a_leap_year_is_exactly_one(self) -> None:
        f = year_fraction(date(2024, 1, 1), date(2025, 1, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(1)

    def test_a_non_leap_year_is_exactly_one(self) -> None:
        f = year_fraction(date(2023, 1, 1), date(2024, 1, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(1)

    def test_multi_year_period_accumulates_each_year_on_its_own_basis(self) -> None:
        f = year_fraction(date(2023, 1, 1), date(2026, 1, 1), DayCount.ACT_ACT_ISDA)
        assert f == Decimal(3)


class TestSymmetry:
    @pytest.mark.parametrize("convention", list(DayCount))
    def test_zero_length_period_is_zero(self, convention: DayCount) -> None:
        assert year_fraction(date(2026, 6, 1), date(2026, 6, 1), convention) == 0

    @pytest.mark.parametrize("convention", list(DayCount))
    def test_reversal_negates(self, convention: DayCount) -> None:
        start, end = date(2025, 3, 10), date(2026, 9, 20)
        assert year_fraction(end, start, convention) == -year_fraction(start, end, convention)
