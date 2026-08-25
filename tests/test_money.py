from decimal import Decimal

import pytest

from capstack.money import (
    ZERO,
    is_close,
    money,
    quantize,
    rate,
    rescale,
    safe_div,
    to_float,
)


class TestMoneyConstruction:
    def test_int_is_exact(self) -> None:
        assert money(250_000_000) == Decimal("250000000")

    def test_string_is_exact(self) -> None:
        assert money("1234.56") == Decimal("1234.56")

    def test_float_goes_through_repr_not_binary_value(self) -> None:
        # The whole point of the module. Decimal(0.1) is 0.1000000000000000055...
        # and money(0.1) must not be.
        assert money(0.1) == Decimal("0.1")
        assert money(0.1) != Decimal(0.1)

    def test_float_repr_round_trip_holds_for_awkward_values(self) -> None:
        for value in (0.07, 0.375, 1.1, 2.675, 0.0575):
            assert money(value) == Decimal(str(value))

    def test_decimal_passes_through_unchanged(self) -> None:
        d = Decimal("3.14159")
        assert money(d) is d

    def test_bool_is_rejected(self) -> None:
        # bool subclasses int, so this would otherwise silently become 1.
        with pytest.raises(TypeError):
            money(True)

    def test_nonsense_string_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            money("not a number")

    def test_nan_and_infinity_are_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                money(bad)

    def test_unsupported_type_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            money([1, 2])  # type: ignore[arg-type]

    def test_rate_is_the_same_coercion(self) -> None:
        assert rate(0.0575) == money("0.0575")


class TestExactness:
    def test_repeated_subtraction_reaches_exactly_zero(self) -> None:
        # A term loan amortising at 1% a quarter for 100 quarters. In float this
        # lands on a residue near 1e-8 rather than zero.
        balance = money(1_000_000)
        instalment = money(10_000)
        for _ in range(100):
            balance -= instalment
        assert balance == ZERO

    def test_tenths_sum_exactly(self) -> None:
        total = sum((money(0.1) for _ in range(10)), ZERO)
        assert total == money(1)


class TestHelpers:
    def test_quantize_rounds_to_cents(self) -> None:
        assert quantize(money("1234.5678")) == Decimal("1234.57")

    def test_quantize_is_half_even(self) -> None:
        # Half-up would bias a schedule that rounds thousands of numbers.
        assert quantize(money("0.125")) == Decimal("0.12")
        assert quantize(money("0.135")) == Decimal("0.14")

    def test_quantize_to_whole_units(self) -> None:
        assert quantize(money("1234.56"), places=0) == Decimal("1235")

    def test_quantize_rejects_negative_places(self) -> None:
        with pytest.raises(ValueError):
            quantize(money(1), places=-1)

    def test_safe_div_normal_case(self) -> None:
        assert safe_div(money(300), money(100)) == money(3)

    def test_safe_div_returns_default_on_zero(self) -> None:
        # A company with no interest expense has no coverage ratio.
        assert safe_div(money(50), ZERO, default=money(0)) == ZERO

    def test_safe_div_raises_without_a_default(self) -> None:
        with pytest.raises(ZeroDivisionError):
            safe_div(money(50), ZERO)

    def test_is_close_within_a_cent(self) -> None:
        assert is_close(money("100.004"), money("100.00"))
        assert not is_close(money("100.02"), money("100.00"))

    def test_is_close_with_explicit_tolerance(self) -> None:
        assert is_close(money(1000), money(1001), tolerance=5)

    def test_to_float_crosses_the_boundary(self) -> None:
        assert to_float(money("0.25")) == 0.25


class TestRescaling:
    def test_the_total_lands_on_the_target_exactly(self) -> None:
        # 1850 into 1440 does not terminate, which is the whole point.
        scaled = rescale(money(1440), [money(0), money(1150), money(450), money(250)])
        assert sum(scaled) == money(1440)

    def test_shares_are_held_to_within_rounding(self) -> None:
        amounts = [money(300), money(700)]
        scaled = rescale(money(500), amounts)
        assert is_close(scaled[0], money(150), tolerance="0.0000001")
        assert sum(scaled) == money(500)

    def test_it_can_grow_as_well_as_shrink(self) -> None:
        assert sum(rescale(money(2000), [money(100), money(900)])) == money(2000)

    def test_a_zero_amount_stays_zero(self) -> None:
        assert rescale(money(500), [money(0), money(1000)])[0] == 0

    def test_resizing_to_nothing_leaves_nothing(self) -> None:
        assert rescale(money(0), [money(100), money(900)]) == [money(0), money(0)]

    def test_a_negative_target_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            rescale(money(-1), [money(100)])

    def test_there_has_to_be_something_to_resize(self) -> None:
        with pytest.raises(ValueError, match="nothing to resize"):
            rescale(money(500), [money(0), money(0)])
