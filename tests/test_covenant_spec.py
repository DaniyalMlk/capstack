"""Reading covenants and a sweep grid out of a deal file, and printing them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capstack.cli import main
from capstack.covenants import Measure
from capstack.money import money
from capstack.spec import DealSpecError, parse_deal

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "meridian.json")


def deal_with(**blocks: Any) -> dict[str, Any]:
    """A deal small enough to reason about, with a real projection behind it."""
    payload: dict[str, Any] = {
        "name": "Test",
        "close_date": "2026-06-30",
        "entry": {"ltm_ebitda": 200, "multiple": 8},
        "debt": [
            {"name": "TLB", "face": 900, "kind": "term_loan", "cash_rate": 0.05},
            {"name": "Notes", "face": 300, "kind": "notes", "cash_rate": 0.08},
        ],
        "structure": {"minimum_cash": 20, "base_rate": 0.03},
        "projection": {"years": 4, "frequency": "annual"},
        "operating": {
            "opening_revenue": 1000,
            "revenue_growth": 0.05,
            "ebitda_margin": 0.22,
            "da_rate": 0.03,
            "capex_rate": 0.035,
            "nwc_rate": 0.1,
            "tax_rate": 0.25,
        },
    }
    for key, value in blocks.items():
        if key == "structure":
            payload["structure"].update(value)
        else:
            payload[key] = value
    return payload


class TestReadingCovenants:
    def test_a_covenant_block_is_read_in_order(self) -> None:
        deal = parse_deal(
            deal_with(
                covenants=[
                    {"name": "Leverage", "measure": "net_leverage", "threshold": 6},
                    {"name": "Cover", "measure": "interest_coverage", "threshold": 2},
                ]
            )
        )
        assert deal.has_covenants
        assert [c.name for c in deal.covenants] == ["Leverage", "Cover"]
        assert deal.covenants[0].measure is Measure.NET_LEVERAGE
        assert deal.covenants[1].measure is Measure.INTEREST_COVERAGE

    def test_a_threshold_series_is_read_against_the_projection(self) -> None:
        deal = parse_deal(
            deal_with(
                covenants=[
                    {
                        "name": "Leverage",
                        "measure": "leverage",
                        "threshold": {"ramp": [7, 5.5]},
                    }
                ]
            )
        )
        covenant = deal.covenants[0]
        assert len(covenant.threshold) == 4
        assert covenant.threshold_at(0) == money(7)
        assert covenant.threshold_at(3) == money("5.5")

    def test_a_test_holiday_is_read(self) -> None:
        deal = parse_deal(
            deal_with(
                covenants=[
                    {
                        "name": "Leverage",
                        "measure": "leverage",
                        "threshold": 6,
                        "first_test_period": 3,
                    }
                ]
            )
        )
        assert deal.covenants[0].first_test_period == 3

    def test_named_tranches_are_read(self) -> None:
        deal = parse_deal(
            deal_with(
                covenants=[
                    {
                        "name": "First lien",
                        "measure": "net_leverage",
                        "threshold": 4,
                        "tranches": ["TLB"],
                    }
                ]
            )
        )
        assert deal.covenants[0].tranches == ("TLB",)

    def test_an_unknown_measure_names_the_ones_that_exist(self) -> None:
        with pytest.raises(DealSpecError, match="unknown measure"):
            parse_deal(
                deal_with(
                    covenants=[{"name": "X", "measure": "vibes", "threshold": 3}]
                )
            )

    def test_a_missing_threshold_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="threshold"):
            parse_deal(deal_with(covenants=[{"name": "X", "measure": "leverage"}]))

    def test_a_covenant_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match=r"covenants\[0\]: expected an object"):
            parse_deal(deal_with(covenants=["leverage"]))

    def test_covenants_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of tests"):
            parse_deal(deal_with(covenants={"name": "X"}))

    def test_a_covenant_needs_something_to_test(self) -> None:
        payload = deal_with(
            covenants=[{"name": "X", "measure": "leverage", "threshold": 6}]
        )
        payload.pop("structure")
        with pytest.raises(DealSpecError, match="nothing to test"):
            parse_deal(payload)

    def test_a_covenant_needs_an_operating_case(self) -> None:
        payload = deal_with(
            covenants=[{"name": "X", "measure": "leverage", "threshold": 6}]
        )
        payload.pop("projection")
        payload.pop("operating")
        with pytest.raises(DealSpecError, match="projection is required"):
            parse_deal(payload)

    def test_a_deal_with_no_covenants_says_so_rather_than_passing(self) -> None:
        deal = parse_deal(deal_with())
        assert not deal.has_covenants
        with pytest.raises(DealSpecError, match="no covenants"):
            deal.test_covenants()

    def test_a_validation_failure_names_the_covenant(self) -> None:
        with pytest.raises(DealSpecError, match=r"covenants\[0\]"):
            parse_deal(
                deal_with(
                    covenants=[
                        {
                            "name": "Cover",
                            "measure": "interest_coverage",
                            "threshold": 2,
                            "tranches": ["TLB"],
                        }
                    ]
                )
            )


class TestReadingASweepGrid:
    def test_a_grid_is_read_and_sorted(self) -> None:
        deal = parse_deal(
            deal_with(structure={"sweep_grid": {"steps": [[3.5, 0.25], [4.5, 0.5]]}})
        )
        assert deal.structure is not None
        grid = deal.structure.sweep_grid
        assert grid is not None
        assert [s.leverage for s in grid.steps] == [money("4.5"), money("3.5")]

    def test_a_floor_and_a_gross_measure_are_read(self) -> None:
        deal = parse_deal(
            deal_with(
                structure={
                    "sweep_grid": {
                        "steps": [[4.5, 0.5]],
                        "floor": 0.1,
                        "net": False,
                    }
                }
            )
        )
        assert deal.structure is not None
        grid = deal.structure.sweep_grid
        assert grid is not None
        assert grid.floor == money("0.1")
        assert grid.net is False

    def test_steps_must_be_pairs(self) -> None:
        with pytest.raises(DealSpecError, match=r"expected a pair"):
            parse_deal(deal_with(structure={"sweep_grid": {"steps": [[4.5]]}}))

    def test_steps_are_required(self) -> None:
        with pytest.raises(DealSpecError, match="steps"):
            parse_deal(deal_with(structure={"sweep_grid": {"floor": 0}}))

    def test_a_grid_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="expected an object"):
            parse_deal(deal_with(structure={"sweep_grid": [[4.5, 0.5]]}))

    def test_a_grid_alongside_a_flat_rate_is_refused(self) -> None:
        with pytest.raises(DealSpecError, match="two different things"):
            parse_deal(
                deal_with(
                    structure={
                        "sweep_rate": 0.5,
                        "sweep_grid": {"steps": [[4.5, 0.5]]},
                    }
                )
            )

    def test_a_grid_changes_what_the_schedule_repays(self) -> None:
        full = parse_deal(deal_with()).schedule()
        stepped = parse_deal(
            deal_with(structure={"sweep_grid": {"steps": [[6.0, 0.5]]}})
        ).schedule()
        assert stepped.total_repaid < full.total_repaid
        assert stepped.closing_cash > full.closing_cash
        assert all(row.reconciles() for row in stepped)

    def test_the_first_step_is_certified_on_the_ltm_figure(self) -> None:
        # 1,200 of debt less 20 of opening cash against 200 of LTM EBITDA is
        # 5.90x, which clears a 6.00x rung and lands on the one below it.
        deal = parse_deal(
            deal_with(
                structure={
                    "sweep_grid": {"steps": [[6.0, 1.0], [3.0, 0.25]]},
                    "opening_cash": 20,
                }
            )
        )
        schedule = deal.schedule()
        assert schedule[0].certified_leverage == money("5.90")
        assert schedule[0].sweep_rate == money("0.25")


class TestTheCovenantsCommand:
    def test_the_worked_example_reports_every_test(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["covenants", EXAMPLE])
        out = capsys.readouterr().out
        assert code == 0
        assert "Total net leverage" in out
        assert "First lien net leverage" in out
        assert "Interest coverage" in out
        assert "Fixed charge coverage" in out
        assert "No maintenance test is breached" in out

    def test_a_test_holiday_prints_as_untested_rather_than_as_a_pass(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["covenants", EXAMPLE])
        out = capsys.readouterr().out
        assert code == 0
        # Period one is inside the holiday on every test in the example.
        status = [line for line in out.splitlines() if "status" in line]
        assert status
        for line in status:
            assert line.split()[1] == "-"

    def test_the_tightest_test_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["covenants", EXAMPLE])
        out = capsys.readouterr().out
        assert "Tightest test" in out
        assert "Breaches below" in out

    def test_json_carries_every_observation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["covenants", EXAMPLE, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert len(payload["covenants"]) == 4
        assert len(payload["observations"]) == 20
        assert payload["passes"] is True
        assert payload["breaches"] == []
        assert payload["first_breach"] is None
        assert payload["tightest"]["covenant"] == "Fixed charge coverage"

    def test_an_observation_carries_the_cushion_and_the_breaking_point(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["covenants", EXAMPLE, "--json"])
        payload = json.loads(capsys.readouterr().out)
        tested = [o for o in payload["observations"] if o["tested"]]
        assert tested
        for row in tested:
            assert row["actual"] is not None
            assert row["ebitda_at_breach"] is not None
            assert row["ebitda_cushion"] is not None
            assert row["headroom"] is not None

    def test_a_breach_is_named_with_its_period(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = deal_with(
            covenants=[
                {"name": "Impossible", "measure": "leverage", "threshold": 0.5}
            ]
        )
        path = tmp_path / "breach.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        code = main(["covenants", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "BREACH" in out
        assert "Impossible is breached in period 1" in out
        assert "4 of 4 tests fail" in out

    def test_a_deal_without_covenants_is_reported_rather_than_crashed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "bare.json"
        path.write_text(json.dumps(deal_with()), encoding="utf-8")
        code = main(["covenants", str(path)])
        assert code == 1
        assert "no covenants" in capsys.readouterr().err
