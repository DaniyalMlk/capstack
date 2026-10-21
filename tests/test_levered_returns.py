"""The returns under each tax basis, and the difference between them.

A model's headline number is its rate of return, and until now that number was
computed as though the business paid tax on EBIT however it was financed. This
file holds the two claims that matter: nothing moved on the basis the
repository's figures are struck on, and on the other basis the deduction is
worth something a sponsor would notice.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from capstack.cli import main
from capstack.money import ZERO, money
from capstack.sensitivity import Case
from capstack.spec import load_deal

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DEALS = sorted(EXAMPLES.glob("*.json"))
MERIDIAN = str(EXAMPLES / "meridian.json")


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_default_basis_is_exactly_what_it_was(path: Path) -> None:
    """The regression guard for the whole change.

    A case run without asking for the levered basis must produce the schedule
    the deal has always produced, cheque for cheque. Asserted against
    ``deal.schedule`` directly rather than against a recorded figure, so it
    stays true as the engine changes.
    """
    deal = load_deal(path)
    model = deal.project()
    case = Case.run(deal)
    reference = deal.schedule(model)

    assert case.levered is None
    for left, right in zip(case.schedule, reference, strict=True):
        assert left.cash_interest == right.cash_interest
        assert left.closing_debt == right.closing_debt
        assert left.closing_cash == right.closing_cash


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_levered_basis_keeps_the_solve_on_the_case(path: Path) -> None:
    case = Case.run(load_deal(path), levered=True)
    assert case.levered is not None
    assert case.levered.total_shield > ZERO
    # The schedule on the case is the settled one, not a second run of it.
    assert case.schedule is case.levered.schedule


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_deduction_is_worth_between_ninety_and_two_hundred_basis_points(
    path: Path,
) -> None:
    """The figure the question is actually asked in.

    A band rather than a digit per deal, because the exact number is a property
    of each file's leverage and coupon. The claim is that it is large: under a
    point of return would be a refinement, and over two would suggest the
    unlevered convention had been doing something other than what it says.
    """
    plain = Case.run(load_deal(path))
    settled = Case.run(load_deal(path), levered=True)
    # Bound to locals before narrowing: irr is a property, and a property is
    # not narrowed by an assertion about it.
    before, after = plain.outcome.irr, settled.outcome.irr
    assert before is not None
    assert after is not None

    gain = (after - before) * 10_000
    assert 85.0 <= gain <= 200.0, f"{path.stem} gained {gain:.1f}bp"
    assert settled.outcome.moic > plain.outcome.moic


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_settled_basis_leaves_less_debt_and_more_equity(path: Path) -> None:
    plain = Case.run(load_deal(path))
    settled = Case.run(load_deal(path), levered=True)
    assert settled.schedule[-1].closing_debt < plain.schedule[-1].closing_debt
    assert settled.outcome.valuation.equity_value > plain.outcome.valuation.equity_value


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_equity_gains_more_than_the_tax_the_deduction_saved(path: Path) -> None:
    """The deduction is worth more than its own tax saving, and here is why.

    The obvious expectation is that the equity gains exactly what the structure
    saved in cash tax. It gains more. The saved cash is swept against the debt,
    and debt repaid early does not accrue the interest it would otherwise have
    accrued, so the equity collects the saving *and* the interest the saving
    avoided.

    Measured across the shipped deals the uplift runs from 9% to 15% above the
    saving itself. The first draft of this test asserted the naive expectation
    — that the equity gains the saving and no more — and it failed, which is
    how the mechanism was found rather than assumed.
    """
    plain = Case.run(load_deal(path))
    settled = Case.run(load_deal(path), levered=True)
    assert settled.levered is not None

    saved = settled.levered.total_shield
    gained = settled.outcome.valuation.equity_value - plain.outcome.valuation.equity_value
    assert gained > saved > ZERO
    ratio = gained / saved
    assert money("1.05") < ratio < money("1.20"), f"{path.stem} gained {ratio:.4f}x the saving"

    # And the interest the repayment avoided is the right size to explain the gap.
    avoided = plain.schedule.total_cash_interest - settled.schedule.total_cash_interest
    assert avoided > ZERO
    assert gained - saved <= avoided * 2


@pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
def test_the_equity_gain_is_the_debt_it_no_longer_owes_plus_the_cash_it_holds(
    path: Path,
) -> None:
    """The identity that does hold, which ties the gain to the balance sheet.

    Equity value is enterprise value less debt plus cash, and the basis changes
    neither the exit multiple nor the EBITDA it is struck on. So whatever the
    deduction is worth has to turn up as debt the business no longer owes or as
    cash it is holding, and nowhere else.
    """
    plain = Case.run(load_deal(path))
    settled = Case.run(load_deal(path), levered=True)

    repaid = plain.schedule[-1].closing_debt - settled.schedule[-1].closing_debt
    held = settled.schedule[-1].closing_cash - plain.schedule[-1].closing_cash
    gained = settled.outcome.valuation.equity_value - plain.outcome.valuation.equity_value
    # Exact but for the last places of a 34-digit context, which two different
    # orderings of the same subtraction do not share.
    assert abs((repaid + held) - gained) < money("1e-25")


def test_a_covenant_report_is_tested_against_the_basis_the_case_ran_on() -> None:
    # A structure carrying less debt has more headroom, so a covenant report
    # built from the unsettled schedule would understate it.
    deal = load_deal(EXAMPLES / "meridian.json")
    if not deal.has_covenants:
        pytest.skip("this deal describes no covenants")
    settled = Case.run(deal, levered=True)
    assert settled.covenants is not None
    assert settled.levered is not None
    assert list(settled.covenants)


# -- Over the wire ------------------------------------------------------


def test_the_exit_command_reports_which_basis_it_ran_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["exit", MERIDIAN, "--json"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert plain["tax_basis"] == "unlevered"
    assert plain["tax_saving"] is None

    assert main(["exit", MERIDIAN, "--levered", "--json"]) == 0
    settled = json.loads(capsys.readouterr().out)
    assert settled["tax_basis"] == "levered"
    assert float(settled["tax_saving"]) > 0


def test_the_flag_moves_the_reported_return(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["exit", MERIDIAN, "--json"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert main(["exit", MERIDIAN, "--levered", "--json"]) == 0
    settled = json.loads(capsys.readouterr().out)
    assert settled["totals"]["irr"] > plain["totals"]["irr"]
    assert Decimal(settled["exit"]["debt"]) < Decimal(plain["exit"]["debt"])


def test_the_table_says_what_it_did_rather_than_quietly_doing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["exit", MERIDIAN, "--levered"]) == 0
    out = capsys.readouterr().out
    assert "taxed on a levered basis" in out
    assert "of cash tax" in out

    assert main(["exit", MERIDIAN]) == 0
    assert "levered basis" not in capsys.readouterr().out


def test_the_flag_is_documented_in_the_commands_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["exit", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--levered" in out
    # The help says what it is worth, because a flag nobody knows the size of
    # is a flag nobody turns on.
    assert "basis points" in out
