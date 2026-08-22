from decimal import Decimal

import pytest

from capstack.drivers import Driver
from capstack.money import money


class TestConstruction:
    def test_explicit_values(self) -> None:
        d = Driver.of(["0.08", "0.07", "0.06"])
        assert list(d) == [money("0.08"), money("0.07"), money("0.06")]
        assert len(d) == 3

    def test_constant(self) -> None:
        d = Driver.constant("0.20", 4)
        assert list(d) == [money("0.20")] * 4

    def test_ramp_hits_both_ends_exactly(self) -> None:
        # "Growth tapers from 9% to 3% over five years" means the fifth year
        # is 3%, not something approaching it.
        d = Driver.ramp("0.09", "0.03", 5)
        assert list(d) == [
            money("0.09"),
            money("0.075"),
            money("0.06"),
            money("0.045"),
            money("0.03"),
        ]

    def test_ramp_upward(self) -> None:
        d = Driver.ramp("0.18", "0.22", 5)
        assert d[0] == money("0.18")
        assert d[-1] == money("0.22")
        assert d[2] == money("0.20")

    def test_ramp_over_one_period_is_the_start(self) -> None:
        assert list(Driver.ramp(5, 99, 1)) == [money(5)]

    def test_ramp_between_equal_values_is_constant(self) -> None:
        assert list(Driver.ramp("0.05", "0.05", 4)) == [money("0.05")] * 4

    def test_ramp_steps_are_even(self) -> None:
        d = Driver.ramp(0, 1, 11)
        steps = {d[i + 1] - d[i] for i in range(10)}
        assert steps == {Decimal("0.1")}

    def test_empty_driver_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one value"):
            Driver(values=())

    @pytest.mark.parametrize("periods", [0, -1])
    def test_non_positive_period_counts_are_rejected(self, periods: int) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            Driver.constant(1, periods)
        with pytest.raises(ValueError, match="at least one period"):
            Driver.ramp(1, 2, periods)


class TestLookup:
    def test_indexing(self) -> None:
        d = Driver.of([1, 2, 3])
        assert d[0] == money(1)
        assert d[-1] == money(3)

    def test_at_within_range(self) -> None:
        d = Driver.of([1, 2, 3])
        assert d.at(1) == money(2)

    def test_at_beyond_the_end_holds_the_last_value(self) -> None:
        # A five-year assumption on a six-year projection repeats its final
        # year rather than taking down the model.
        d = Driver.of([1, 2, 3])
        assert d.at(3) == money(3)
        assert d.at(99) == money(3)

    def test_negative_index_is_rejected_by_at(self) -> None:
        with pytest.raises(IndexError, match="must not be negative"):
            Driver.of([1, 2, 3]).at(-1)

    def test_extended_to_a_longer_horizon(self) -> None:
        assert list(Driver.of([1, 2]).extended_to(4)) == [
            money(1),
            money(2),
            money(2),
            money(2),
        ]

    def test_extended_to_a_shorter_horizon_truncates(self) -> None:
        assert list(Driver.of([1, 2, 3, 4]).extended_to(2)) == [money(1), money(2)]

    def test_extended_to_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one period"):
            Driver.of([1, 2]).extended_to(0)
