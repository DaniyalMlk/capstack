import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from capstack.money import money
from capstack.spec import DealSpecError, load_deal, parse_deal

MINIMAL = {"entry": {"ltm_ebitda": 100, "multiple": 10}}


def write(tmp_path: Path, payload: object, name: str = "deal.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestParsing:
    def test_minimal_deal(self) -> None:
        name, close, deal = parse_deal(dict(MINIMAL))
        assert name == "Untitled"
        assert close is None
        assert deal.valuation.enterprise_value == money(1000)

    def test_name_and_close_date(self) -> None:
        name, close, _ = parse_deal(
            {**MINIMAL, "name": "Project Meridian", "close_date": "2026-06-30"}
        )
        assert name == "Project Meridian"
        assert close == date(2026, 6, 30)

    def test_tranches_are_read_in_order(self) -> None:
        _, _, deal = parse_deal(
            {
                **MINIMAL,
                "debt": [
                    {"name": "TLB", "face": 400, "issue_price": "0.995"},
                    {"name": "Notes", "face": 200},
                ],
            }
        )
        assert [t.name for t in deal.debt] == ["TLB", "Notes"]
        assert deal.debt[0].proceeds == money(398)

    def test_tranche_defaults_to_par_with_no_fee(self) -> None:
        _, _, deal = parse_deal({**MINIMAL, "debt": [{"name": "TLB", "face": 400}]})
        assert deal.debt[0].issue_price == money(1)
        assert deal.debt[0].financing_fee == money(0)

    def test_other_uses_are_read(self) -> None:
        _, _, deal = parse_deal(
            {**MINIMAL, "other_uses": [{"label": "Break fee", "amount": 12, "note": "payable"}]}
        )
        assert deal.other_uses[0].label == "Break fee"
        assert deal.other_uses[0].amount == money(12)
        assert deal.other_uses[0].note == "payable"

    def test_optional_amounts_default_to_zero(self) -> None:
        _, _, deal = parse_deal(dict(MINIMAL))
        assert deal.rollover_equity == money(0)
        assert deal.transaction_fee_rate == money(0)

    def test_explicit_null_is_treated_as_absent(self) -> None:
        _, _, deal = parse_deal({**MINIMAL, "rollover_equity": None})
        assert deal.rollover_equity == money(0)


class TestExactness:
    def test_decimal_literals_survive_the_round_trip(self, tmp_path: Path) -> None:
        # The reason for parse_float=Decimal. Read as a float, 0.995 times a
        # face of 400,000,000 misses by a fraction of a cent; read as a decimal
        # it is exactly 398,000,000.
        path = write(
            tmp_path,
            {
                "entry": {"ltm_ebitda": 100, "multiple": 10},
                "debt": [{"name": "TLB", "face": 400000000, "issue_price": 0.995}],
            },
        )
        _, _, deal = load_deal(path)
        assert deal.debt[0].issue_price == Decimal("0.995")
        assert deal.debt[0].proceeds == Decimal("398000000.000")

    def test_a_loaded_deal_still_balances_exactly(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            {
                "entry": {
                    "ltm_ebitda": 87.3,
                    "multiple": 11.75,
                    "existing_debt": 37.77,
                    "existing_cash": 4.13,
                },
                "debt": [
                    {"name": "TLB", "face": 512.37, "issue_price": 0.9925, "financing_fee_rate": 0.0175}
                ],
                "rollover_equity": 19.19,
                "cash_from_balance_sheet": 4.13,
                "transaction_fee_rate": 0.0137,
            },
        )
        _, _, deal = load_deal(path)
        table = deal.sources_and_uses()
        assert table.total_sources - table.total_uses == money(0)


class TestLoadErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(DealSpecError, match="cannot read"):
            load_deal(tmp_path / "absent.json")

    def test_invalid_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text('{\n  "entry": {,\n}', encoding="utf-8")
        with pytest.raises(DealSpecError, match="invalid JSON at line 2"):
            load_deal(path)

    def test_top_level_must_be_an_object(self, tmp_path: Path) -> None:
        with pytest.raises(DealSpecError, match="object at the top level"):
            load_deal(write(tmp_path, [1, 2, 3]))

    def test_missing_entry_block(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'entry'"):
            parse_deal({"name": "x"})

    def test_missing_ebitda(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'ltm_ebitda'"):
            parse_deal({"entry": {"multiple": 10}})

    def test_missing_multiple(self) -> None:
        with pytest.raises(DealSpecError, match="missing required field 'multiple'"):
            parse_deal({"entry": {"ltm_ebitda": 100}})

    def test_entry_must_be_an_object(self) -> None:
        with pytest.raises(DealSpecError, match="entry: expected an object"):
            parse_deal({"entry": 100})

    def test_debt_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="expected a list of tranches"):
            parse_deal({**MINIMAL, "debt": {"name": "TLB"}})

    def test_a_tranche_must_be_an_object(self) -> None:
        with pytest.raises(DealSpecError, match=r"debt\[0\]: expected an object"):
            parse_deal({**MINIMAL, "debt": ["TLB"]})

    def test_a_tranche_needs_a_face_amount(self) -> None:
        with pytest.raises(DealSpecError, match=r"debt\[1\]: missing required field 'face'"):
            parse_deal({**MINIMAL, "debt": [{"name": "A", "face": 1}, {"name": "B"}]})

    def test_a_bad_number_names_its_field(self) -> None:
        with pytest.raises(DealSpecError, match="entry.existing_debt: not a number"):
            parse_deal({"entry": {"ltm_ebitda": 100, "multiple": 10, "existing_debt": "lots"}})

    def test_a_bad_close_date(self) -> None:
        with pytest.raises(DealSpecError, match="close_date: not a date"):
            parse_deal({**MINIMAL, "close_date": "30 June 2026"})

    def test_other_uses_must_be_a_list(self) -> None:
        with pytest.raises(DealSpecError, match="other_uses: expected a list"):
            parse_deal({**MINIMAL, "other_uses": {"label": "x", "amount": 1}})

    def test_an_other_use_needs_an_amount(self) -> None:
        with pytest.raises(DealSpecError, match=r"other_uses\[0\]: missing required field 'amount'"):
            parse_deal({**MINIMAL, "other_uses": [{"label": "Break fee"}]})

    def test_domain_validation_still_applies_after_parsing(self) -> None:
        # The spec layer parses; the transaction layer is what rejects nonsense.
        with pytest.raises(ValueError, match="positive EBITDA"):
            parse_deal({"entry": {"ltm_ebitda": 0, "multiple": 10}})


class TestShippedExample:
    def test_the_worked_example_loads_and_balances(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        name, close, deal = load_deal(path)
        assert name == "Project Meridian"
        assert close == date(2026, 6, 30)

        # Checked by hand against the file:
        #   EV            240 x 11.5              = 2,760.00
        #   Net debt      410 - 65                =   345.00
        #   Equity price  2,760 - 345             = 2,415.00
        #   Fin. fees     25.875 + 7.875 + 7.5    =    41.25
        #   OID           5.75 + 5.00             =    10.75
        #   Txn fees      2,760 x 1.4%            =    38.64
        #   Uses          2,415 + 410 + 38.64 + 41.25 + 10.75 + 40 + 18.5 = 2,974.14
        #   Sources       1,850 + 85 + 45 + sponsor           -> sponsor =   994.14
        assert deal.valuation.enterprise_value == money("2760.0")
        assert deal.valuation.equity_purchase_price == money("2415.0")
        assert deal.financing_fees == money("41.25")
        assert deal.original_issue_discount == money("10.75")
        assert deal.transaction_fees == money("38.64")
        assert deal.sponsor_equity == money("994.14")

        table = deal.sources_and_uses()
        assert table.total_uses == money("2974.14")
        assert table.total_sources == money("2974.14")

    def test_the_worked_example_entry_metrics(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        _, _, deal = load_deal(path)
        assert deal.total_debt == money("1850.0")
        assert deal.entry_leverage == money("1850.0") / money("240.0")
        assert round(float(deal.entry_leverage), 2) == 7.71
        assert deal.total_capitalisation == money("2929.14")
        assert round(float(deal.equity_contribution_rate), 4) == 0.3684
        assert round(float(deal.sponsor_ownership), 4) == 0.9212

    def test_the_undrawn_revolver_is_left_off_the_sources_side(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "meridian.json"
        _, _, deal = load_deal(path)
        labels = [i.label for i in deal.sources_and_uses().sources]
        assert "Revolving credit facility" not in labels
        # ...but it is still part of the structure.
        assert any(t.name == "Revolving credit facility" for t in deal.debt)
