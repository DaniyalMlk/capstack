"""The deduction reached from a deal file and from the command line.

A tool that computes the right answer and cannot be asked for it is half a
tool, so this file drives the whole thing the way a user does: a document in,
a table or a JSON object out, and a readable error when the document is wrong.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.money import ZERO, money
from capstack.spec import Deal, DealSpecError, load_deal, parse_deal
from capstack.tax import EarningsBase, InterestLimit

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MERIDIAN = str(EXAMPLES / "meridian.json")
THORNBURY = EXAMPLES / "thornbury.json"


def document(limit: dict[str, Any] | None = None) -> dict[str, Any]:
    """A real deal document, optionally with a cap written into it."""
    data: dict[str, Any] = deepcopy(json.loads(THORNBURY.read_text(encoding="utf-8")))
    if limit is not None:
        data["operating"]["interest_limit"] = limit
    return data


# -- The deal file ------------------------------------------------------


def test_a_file_with_no_cap_block_runs_the_deduction_uncapped() -> None:
    deal = parse_deal(document())
    assert deal.interest_limit is None
    assert deal.tax_policy.interest_limit is None
    solution = deal.levered()
    assert solution.tax.closing_disallowed_interest == ZERO


def test_the_cap_is_read_off_the_file_and_reaches_the_answer() -> None:
    deal = parse_deal(document({"rate": 0.30, "base": "ebitda"}))
    assert deal.interest_limit == InterestLimit(rate=money("0.30"), base=EarningsBase.EBITDA)

    capped = deal.levered()
    uncapped = parse_deal(document()).levered()
    assert capped.tax.total_interest_deducted <= uncapped.tax.total_interest_deducted


def test_a_tight_cap_in_the_file_bites_and_the_pool_shows_it() -> None:
    deal = parse_deal(document({"rate": 0.05, "base": "ebit"}))
    solution = deal.levered()
    assert solution.tax.binding_periods
    assert solution.tax.closing_disallowed_interest > ZERO
    assert solution.tax.deducted_share < money(1)


def test_the_base_defaults_to_ebitda_when_the_file_names_only_a_rate() -> None:
    deal = parse_deal(document({"rate": 0.30}))
    assert deal.interest_limit is not None
    assert deal.interest_limit.base is EarningsBase.EBITDA


def test_the_base_is_read_case_insensitively() -> None:
    deal = parse_deal(document({"rate": 0.30, "base": "EBIT"}))
    assert deal.interest_limit is not None
    assert deal.interest_limit.base is EarningsBase.EBIT


def test_the_tax_policy_takes_its_rate_from_the_operating_case() -> None:
    deal = parse_deal(document())
    assert deal.operating is not None
    assert deal.tax_policy.rate == deal.operating.tax_rate
    assert deal.tax_policy.nol_usage_limit == deal.operating.nol_usage_limit


def test_a_model_already_in_hand_is_used_rather_than_projected_again() -> None:
    deal = parse_deal(document())
    model = deal.project()
    with_model = deal.levered(model)
    without = deal.levered()
    assert with_model.tax.cash_taxes == without.tax.cash_taxes


# -- What the file is not allowed to say --------------------------------


@pytest.mark.parametrize(
    ("limit", "fragment"),
    [
        ({"base": "ebitda"}, "missing required field 'rate'"),
        ({"rate": 0.3, "base": "turnover"}, "expected one of"),
        ({"rate": -0.3}, "must not be negative"),
    ],
)
def test_a_cap_the_regime_cannot_mean_is_refused_with_the_path_named(
    limit: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(DealSpecError) as exc:
        parse_deal(document(limit))
    assert "operating.interest_limit" in str(exc.value)
    assert fragment in str(exc.value)


def test_a_cap_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(DealSpecError) as exc:
        parse_deal(document([0.3]))  # type: ignore[arg-type]
    assert "expected an object" in str(exc.value)


def test_the_error_names_the_bases_the_regime_does_allow() -> None:
    # A message a reader can act on without opening the source.
    with pytest.raises(DealSpecError) as exc:
        parse_deal(document({"rate": 0.3, "base": "revenue"}))
    assert "'ebitda'" in str(exc.value)
    assert "'ebit'" in str(exc.value)


def test_a_deal_with_no_structure_says_so_rather_than_failing_obscurely() -> None:
    deal = load_deal(Path(__file__).resolve().parent / "data" / "anchor.json")
    stripped = Deal(
        name=deal.name,
        transaction=deal.transaction,
        close_date=deal.close_date,
        grid=deal.grid,
        operating=deal.operating,
        opening_revenue=deal.opening_revenue,
    )
    with pytest.raises(DealSpecError) as exc:
        stripped.levered()
    assert "no capital structure" in str(exc.value)


def test_a_deal_with_no_operating_case_says_so() -> None:
    deal = load_deal(Path(__file__).resolve().parent / "data" / "anchor.json")
    bare = Deal(name=deal.name, transaction=deal.transaction)
    with pytest.raises(DealSpecError) as exc:
        _ = bare.tax_policy
    assert "no operating case" in str(exc.value)


# -- Over the wire ------------------------------------------------------


def test_the_command_prints_the_deduction_the_pool_and_the_saving(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tax", MERIDIAN]) == 0
    out = capsys.readouterr().out
    assert "tax on the financed business" in out
    for label in (
        "Interest arising",
        "Interest deducted",
        "Disallowed, carried",
        "Cash tax",
        "Tax if unlevered",
        "Saving",
    ):
        assert label in out
    # The solve reports its own work rather than hiding it.
    assert "passes, residual" in out
    assert "Deduction uncapped" in out


def test_the_command_emits_json_a_consumer_can_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tax", MERIDIAN, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["limit"] is None
    assert report["iterations"] >= 1
    assert len(report["periods"]) == len(report["periods"])
    first = report["periods"][0]
    for key in ("ebitda", "ebit", "interest_arising", "cash_tax", "shield"):
        Decimal(first[key])
    totals = report["totals"]
    # The saving ties to the two totals it is a difference of.
    assert Decimal(totals["shield"]) == Decimal(totals["cash_tax_unlevered"]) - Decimal(
        totals["cash_tax"]
    )
    assert 0.0 <= totals["deducted_share"] <= 1.0


def test_a_capped_deal_reports_the_cap_and_the_periods_it_bound_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "capped.json"
    path.write_text(json.dumps(document({"rate": 0.05, "base": "ebit"})), encoding="utf-8")
    assert main(["tax", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Deduction capped at 5% of EBIT" in out
    assert "Cap bound in" in out
    assert "Still disallowed at exit" in out


def test_a_bad_cap_in_the_file_is_an_error_a_caller_can_act_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(document({"base": "ebitda"})), encoding="utf-8")
    assert main(["tax", str(path)]) == 1
    err = capsys.readouterr().err
    assert "capstack:" in err
    assert "operating.interest_limit.rate" in err or "operating.interest_limit" in err
    # Not a traceback.
    assert "Traceback" not in err


def test_a_missing_file_is_an_error_rather_than_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tax", "no/such/deal.json"]) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Traceback" not in err


def test_the_command_renders_its_own_help(capsys: pytest.CaptureFixture[str]) -> None:
    # The whole-parser sweep in test_cli_help covers this too; asserted here as
    # well because a command whose help crashes is useless in exactly the
    # moment a user needs it.
    with pytest.raises(SystemExit) as exc:
        main(["tax", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "deal file" in out
    assert "--json" in out


@pytest.mark.parametrize(
    ("rate", "shown"),
    [(0.30, "30%"), (0.05, "5%"), (0.075, "7.5%"), (0.3125, "31.25%"), (1, "100%")],
)
def test_the_cap_is_printed_without_trailing_zeros_or_an_exponent(
    rate: float, shown: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Decimal.normalize() renders 30.00 as 3E+1, which is how a cap ends up
    # printed in scientific notation. The zeros are stripped by hand instead.
    path = tmp_path / "rate.json"
    path.write_text(json.dumps(document({"rate": rate})), encoding="utf-8")
    assert main(["tax", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"Deduction capped at {shown} of EBITDA" in out
    assert "E+" not in out
