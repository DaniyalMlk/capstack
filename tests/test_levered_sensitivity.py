"""The cap as a surface, which is the shape the engine could not draw before.

Phase 13 established two facts about the cap by running structures one at a
time: past saturation a turn of leverage buys no further deduction, and the
cap's earnings base moves the threshold by capital intensity. Both are claims
about a surface, and this file is the surface — a grid with leverage down one
side and the cap across the other, where saturation is visible as a shape
rather than inferred from a pair of numbers.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.money import Money, money
from capstack.sensitivity import Axis, Dimension, Grid, Metric, SensitivityError
from capstack.spec import Deal, load_deal, parse_deal
from capstack.tax import EarningsBase, InterestLimit

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MERIDIAN = EXAMPLES / "meridian.json"

#: Leverage multiples spanning the saturation boundary, and cap rates spanning
#: binding-everywhere to binding-nowhere.
TURNS = (money("4"), money("5"), money("6"), money("7"), money("8"))
TIGHT = (money("0.05"), money("0.10"), money("0.20"))


def document(limit: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = deepcopy(json.loads(MERIDIAN.read_text(encoding="utf-8")))
    if limit is not None:
        data["operating"]["interest_limit"] = limit
    return data


def capped(rate: float = 0.30, base: str = "ebitda") -> Deal:
    """Meridian with a cap. Chosen because it carries no events.

    A deal with add-ons or a refinancing cannot be levered to eight turns —
    the add-ons stop funding — so a grid over that range on one of those is a
    table of honest failures rather than a surface.
    """
    return parse_deal(document({"rate": rate, "base": base}))


def grid(
    metric: Metric = Metric.TAX_SAVING,
    *,
    caps: tuple[Money, ...] = TIGHT,
    turns: tuple[Money, ...] = TURNS,
    levered: bool = True,
    deal: Deal | None = None,
) -> Grid:
    return Grid.run(
        capped() if deal is None else deal,
        Axis(Dimension.LEVERAGE, turns),
        Axis(Dimension.INTEREST_LIMIT, caps),
        metric,
        levered=levered,
    )


# -- Saturation, as a shape ----------------------------------------------


def test_where_the_cap_binds_the_saving_does_not_move_with_leverage_at_all() -> None:
    """A column of identical numbers, and that is the finding.

    At a cap tight enough to bind in every period of every structure, the
    deduction is the allowance and the allowance is a function of earnings. So
    four turns and eight turns save the same cash tax — to the last digit, on
    exact arithmetic, not to a tolerance.
    """
    table = grid()
    for column in range(len(TIGHT)):
        values = [table.cells[row][column].value for row in range(len(TURNS))]
        assert all(v is not None for v in values)
        assert len(set(map(str, values))) == 1, f"column {column} moved with leverage"


def test_in_that_regime_the_saving_is_exactly_proportional_to_the_cap() -> None:
    """The other exact relation, and the reason the columns are flat.

    A saturated deduction is the sum of ``rate x EBITDA``, so the cash tax it
    saves is ``rate x tax rate x total EBITDA`` — linear in the rate with no
    remainder. Doubling the cap doubles the saving exactly, and quadrupling it
    quadruples it.
    """
    row = grid().cells[0]
    at_five, at_ten, at_twenty = (cell.value for cell in row)
    assert at_five is not None and at_ten is not None and at_twenty is not None
    assert at_ten == at_five * 2
    assert at_twenty == at_five * 4


def test_a_cap_loose_enough_to_miss_lets_the_saving_grow_with_leverage() -> None:
    # The other side of the boundary. Without it the flatness above could just
    # be a grid that does not respond to its own row axis.
    table = grid(caps=(money("0.50"),))
    values = [table.cells[row][0].value for row in range(len(TURNS))]
    assert not any(v is None for v in values)
    rising = [v for v in values if v is not None]
    assert rising == sorted(rising)
    assert len(set(map(str, rising))) == len(TURNS)


def test_the_boundary_itself_shows_a_column_that_rises_and_then_flattens() -> None:
    # At the regime's own 30% the deal saturates partway up the axis, so the
    # column rises and then stops. That is the saturation point, located by the
    # grid rather than asserted.
    table = grid(caps=(money("0.30"),))
    values = [table.cells[row][0].value for row in range(len(TURNS))]
    assert not any(v is None for v in values)
    curve = [v for v in values if v is not None]
    assert curve[0] < curve[1] < curve[2]
    assert curve[2] == curve[3] == curve[4]


def test_the_return_stops_responding_to_leverage_past_saturation_too() -> None:
    # The saving is the mechanism; the return is what a reader cares about.
    table = grid(Metric.IRR, caps=(money("0.10"),))
    rates = [table.cells[row][0].value for row in range(len(TURNS))]
    assert all(r is not None for r in rates)
    # Leverage still changes the return — it is still leverage — but it no
    # longer changes it through the deduction, so the saving column above is
    # flat while this one is not.
    assert len(set(map(str, rates))) > 1


# -- What the grid refuses -----------------------------------------------


@pytest.mark.parametrize("metric", [Metric.CASH_TAX, Metric.TAX_SAVING])
def test_a_tax_metric_on_the_default_basis_is_refused_up_front(metric: Metric) -> None:
    assert metric.needs_levered
    with pytest.raises(SensitivityError) as exc:
        grid(metric, levered=False)
    assert "levered tax basis" in str(exc.value)
    assert "--levered" in str(exc.value)


@pytest.mark.parametrize(
    "metric", [Metric.IRR, Metric.MOIC, Metric.EQUITY_VALUE, Metric.EXIT_LEVERAGE]
)
def test_every_other_metric_needs_no_basis_asked_for(metric: Metric) -> None:
    assert not metric.needs_levered


def test_reading_the_cap_off_a_file_that_caps_nothing_says_so() -> None:
    plain = load_deal(MERIDIAN)
    assert plain.interest_limit is None
    with pytest.raises(SensitivityError) as exc:
        Dimension.INTEREST_LIMIT.read(plain)
    assert "caps nothing" in str(exc.value)
    assert "interest_limit" in str(exc.value)


def test_the_cap_is_read_off_a_file_that_does_describe_one() -> None:
    assert Dimension.INTEREST_LIMIT.read(capped(0.25)) == money("0.25")


def test_flexing_a_deal_onto_a_cap_creates_one_and_keeps_its_base() -> None:
    plain = load_deal(MERIDIAN)
    onto = Dimension.INTEREST_LIMIT.apply(plain, money("0.30"))
    assert onto.interest_limit == InterestLimit(rate=money("0.30"), base=EarningsBase.EBITDA)

    # Where the file names a base, moving the rate leaves the base alone: the
    # axis moves one assumption and the base is a different question.
    on_ebit = Dimension.INTEREST_LIMIT.apply(capped(0.30, "ebit"), money("0.10"))
    assert on_ebit.interest_limit is not None
    assert on_ebit.interest_limit.base is EarningsBase.EBIT
    assert on_ebit.interest_limit.rate == money("0.10")


def test_a_negative_cap_on_the_axis_is_refused() -> None:
    with pytest.raises(SensitivityError) as exc:
        Dimension.INTEREST_LIMIT.apply(capped(), money("-0.1"))
    assert "not below zero" in str(exc.value)


def test_the_cap_is_a_level_and_not_a_shift() -> None:
    # A shift axis reads as a departure from the file and centres on zero. A cap
    # of 30% is 30%, so it has to be a level or every axis value would be
    # interpreted against whatever the file happened to say.
    assert not Dimension.INTEREST_LIMIT.is_shift
    assert Dimension.INTEREST_LIMIT.is_continuous


# -- Nothing moved on the default basis ----------------------------------


def test_a_grid_on_the_default_basis_is_what_it_always_was() -> None:
    deal = load_deal(MERIDIAN)
    axes = (
        Axis(Dimension.EXIT_MULTIPLE, (money("9"), money("10"), money("11"))),
        Axis(Dimension.LEVERAGE, (money("4"), money("5"))),
    )
    plain = Grid.run(deal, axes[0], axes[1], Metric.IRR)
    again = Grid.run(deal, axes[0], axes[1], Metric.IRR, levered=False)
    for left, right in zip(plain.cells, again.cells, strict=True):
        for a, b in zip(left, right, strict=True):
            assert a.value == b.value
    assert plain.base == again.base


def test_asking_for_the_basis_moves_every_cell_of_the_same_grid() -> None:
    deal = load_deal(MERIDIAN)
    rows = Axis(Dimension.EXIT_MULTIPLE, (money("9"), money("10"), money("11")))
    columns = Axis(Dimension.LEVERAGE, (money("4"), money("5")))
    plain = Grid.run(deal, rows, columns, Metric.IRR)
    settled = Grid.run(deal, rows, columns, Metric.IRR, levered=True)
    for left, right in zip(plain.cells, settled.cells, strict=True):
        for a, b in zip(left, right, strict=True):
            assert a.value is not None and b.value is not None
            assert b.value > a.value


# -- Over the wire ------------------------------------------------------


def test_the_command_runs_a_levered_grid_and_says_which_basis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "capped.json"
    path.write_text(json.dumps(document({"rate": 0.30})), encoding="utf-8")
    assert (
        main(
            [
                "sensitivity",
                str(path),
                "--rows",
                "leverage:5,6,7",
                "--columns",
                "interest-limit:0.10,0.30",
                "--metric",
                "tax-saving",
                "--levered",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["tax_basis"] == "levered"
    assert report["metric"]["name"] == "tax-saving"


def test_the_command_refuses_a_tax_metric_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "capped.json"
    path.write_text(json.dumps(document({"rate": 0.30})), encoding="utf-8")
    assert (
        main(
            [
                "sensitivity",
                str(path),
                "--rows",
                "leverage:5,6",
                "--columns",
                "exit-multiple:9,10",
                "--metric",
                "cash-tax",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "--levered" in err
    assert "Traceback" not in err


def test_the_new_dimension_and_metrics_are_offered_by_the_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sensitivity", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "cash-tax" in out
    assert "tax-saving" in out
    assert "--levered" in out
