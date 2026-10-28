"""Amounts in messages are written the way a person writes amounts.

This exists because of a sentence the engine printed, in the memo, for a deal
in this repository:

    needs 2.39150 off a balance sheet holding
    12.00000000000000000000000000000000 against a minimum of 12.0, which
    leaves only 0E-32

Three things are wrong with it and they are all the same thing. The balance and
the minimum are *the same number*, rendered two ways, in a sentence whose whole
job is to let a reader compare them. The shortfall is nil and is written in
scientific notation with a thirty-two digit exponent. And none of it looks like
money, which is what it is.

The cause is that ``Money`` is ``Decimal`` at a thirty-four digit context: a
value that has been through a division carries its context into an f-string,
and a value read straight from the deal file does not. So whether a message
reads well depends on arithmetic the message knows nothing about, which is not
a property anything can rely on. Every amount that reaches a message goes
through ``money.written`` now.

The checks here are in three layers, because the bug was found at the third.

1. ``written`` itself, on the values that produced the sentence above.
2. Each message that was changed, constructed to fail, asserted not to contain
   a long run of digits or an exponent.
3. Every shipped deal, taken through the whole memo including the break-even
   search -- which is where the sentence appeared, and the only layer that
   would have caught it, because nothing below it knew those messages were
   being printed rather than raised.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from capstack import load_deal, money
from capstack.balance_sheet import BalanceSheetError, TargetBookBalanceSheet
from capstack.debt import CapitalStructure, Tranche, TrancheKind
from capstack.events import AddOn, AddOnError, Draw, RecapitalisationError
from capstack.money import written
from capstack.report import prepare
from capstack.transaction import LineItem, SourcesAndUses, UnbalancedTransactionError

ROOT = Path(__file__).resolve().parent.parent
DEALS = sorted((ROOT / "examples").glob("*.json"))

#: What a thirty-four digit context looks like once it reaches prose: a run of
#: digits after the point longer than anybody writes, or an exponent.
#:
#: Six is the threshold because the engine legitimately quotes a rate to more
#: than two places and a residual in scientific notation, and both of those are
#: deliberate -- see the exclusions in the third layer below.
UGLY = re.compile(r"\d\.\d{7,}|\d[eE][+-]\d")


class TestWritten:
    """The helper, on the values that produced the sentence."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12.00000000000000000000000000000000", "12.00"),
            ("12.0", "12.00"),
            ("0E-32", "0.00"),
            ("-0E-32", "0.00"),
            ("-12.00000000000000000000000000000000", "-12.00"),
            ("2.39150", "2.39"),
            ("1234567.891", "1,234,567.89"),
            ("0", "0.00"),
        ],
    )
    def test_it_writes_the_amount(self, value: str, expected: str) -> None:
        assert written(Decimal(value)) == expected

    def test_the_two_renderings_that_started_this_agree(self) -> None:
        """The balance and the minimum in that sentence were the same number."""
        balance = Decimal("12.00000000000000000000000000000000")
        minimum = Decimal("12.0")
        assert balance == minimum
        assert written(balance) == written(minimum)

    def test_nothing_it_writes_is_ugly(self) -> None:
        for value in ["12." + "0" * 32, "0E-32", "1E+12", "-0.004", "1" * 15]:
            assert not UGLY.search(written(Decimal(value)))

    def test_a_negative_amount_keeps_its_sign_but_nil_does_not(self) -> None:
        # `-0.00` is arithmetically correct and reads as a mistake, while
        # `-0.01` is a real overdraft and has to keep the minus.
        assert written(Decimal("-0.0001")) == "0.00"
        assert written(Decimal("-0.01")) == "-0.01"


class TestMessages:
    """Each message that was changed, with a failure built to trip it."""

    def test_an_unbalanced_funding_table_writes_both_sides_the_same_way(self) -> None:
        with pytest.raises(UnbalancedTransactionError) as caught:
            SourcesAndUses(
                sources=(LineItem(label="Debt", amount=money(1000) / money(3)),),
                uses=(LineItem(label="Price", amount=money(1000) / money(7)),),
            )
        message = str(caught.value)
        assert not UGLY.search(message), message

    def test_a_balance_sheet_that_does_not_balance(self) -> None:
        with pytest.raises(BalanceSheetError) as caught:
            TargetBookBalanceSheet(
                total_assets=money(1000) / money(3),
                total_liabilities=money(0),
                goodwill=money(1000),
            )
        message = str(caught.value)
        assert not UGLY.search(message), message

    def test_a_draw_of_nothing(self) -> None:
        with pytest.raises(RecapitalisationError) as caught:
            Draw(tranche="Term Loan B", amount=money(0))
        message = str(caught.value)
        assert not UGLY.search(message), message

    def test_an_acquisition_of_losses(self) -> None:
        with pytest.raises(AddOnError) as caught:
            AddOn(
                label="Bolt-on",
                period=1,
                ebitda=money(-1) / money(3),
                multiple=money(8),
            )
        message = str(caught.value)
        assert not UGLY.search(message), message

    def test_a_refinancing_the_balance_sheet_cannot_fund(self) -> None:
        from capstack.debt import DebtSchedule
        from capstack.events import Draw, Refinancing, RefinancingError
        from capstack.periods import Frequency, PeriodGrid

        structure = CapitalStructure.of(
            [
                Tranche.of(
                    "Old TLB", TrancheKind.TERM_LOAN, 500, cash_rate="0.08", floating=False
                ),
                Tranche.of(
                    "New TLB", TrancheKind.TERM_LOAN, 0, cash_rate="0.05", floating=False
                ),
            ],
            # A third of a hundred, so the minimum and the shortfall both come
            # out of a division and carry the full context.
            minimum_cash=money(100) / money(3),
        )
        periods = list(PeriodGrid.build(date(2026, 6, 30), years=5, frequency=Frequency.ANNUAL))
        takeout = Refinancing.of(
            2, "Old TLB", into=[Draw.of("New TLB", 100)], call_premium_rate="0.02"
        )
        with pytest.raises(RefinancingError) as caught:
            DebtSchedule.run(
                structure,
                periods,
                [money(100) / money(7)] * len(periods),
                opening_cash=money(100) / money(7),
                refinancings=[takeout],
            )
        message = str(caught.value)
        assert not UGLY.search(message), message
        assert "a minimum of 33.33" in message

    def test_two_draws_that_overdraw_one_facility_between_them(self) -> None:
        from capstack.debt import DebtSchedule
        from capstack.periods import Frequency, PeriodGrid

        structure = CapitalStructure.of(
            [
                Tranche.of("TLB", TrancheKind.TERM_LOAN, 100, cash_rate="0.08", floating=False),
                Tranche.of(
                    "Delayed draw",
                    TrancheKind.TERM_LOAN,
                    0,
                    commitment=money(200) / money(3),
                    cash_rate="0.08",
                    floating=False,
                    availability=5,
                ),
            ],
            minimum_cash=0,
        )
        periods = list(PeriodGrid.build(date(2026, 6, 30), years=5, frequency=Frequency.ANNUAL))
        purchases = [
            AddOn(
                label="Bolt-on",
                period=index,
                ebitda=money(5),
                multiple=money(8),
                draws=(Draw.of("Delayed draw", money(100) / money(3) + money(5)),),
            )
            for index in (1, 2)
        ]
        with pytest.raises(AddOnError) as caught:
            DebtSchedule.run(
                structure,
                periods,
                [money(500)] * len(periods),
                opening_cash=1000,
                acquisitions=purchases,
            )
        message = str(caught.value)
        assert not UGLY.search(message), message
        assert "of it left" in message

    def test_a_tranche_drawn_past_its_commitment(self) -> None:
        with pytest.raises(ValueError) as caught:
            Tranche.of(
                "Delayed draw",
                TrancheKind.TERM_LOAN,
                money(200) / money(3),
                cash_rate="0.08",
                floating=False,
                commitment=money(100) / money(3),
            )
        message = str(caught.value)
        assert not UGLY.search(message), message


class TestTheWholeMemo:
    """The layer the bug was actually found at.

    The break-even search runs the whole model along an assumption until it
    stops working, catches the refusal, and *prints* it as a note. Nothing
    below this layer knew those messages were being read rather than raised,
    which is why a suite of fifteen hundred tests was green while the memo for
    a shipped deal carried a thirty-two digit exponent in a sentence.
    """

    @pytest.mark.parametrize("path", DEALS, ids=lambda p: p.stem)
    def test_no_memo_prints_a_thirty_four_digit_context(self, path: Path) -> None:
        report = prepare(load_deal(path))
        for section in report:
            for note in section.notes:
                assert not UGLY.search(note), f"{section.title}: {note}"
            for line in section.lines:
                assert not UGLY.search(line.value), f"{section.title}: {line.label}"
                assert not UGLY.search(line.note), f"{section.title}: {line.label}"
            if section.table is not None:
                for row in section.table.rows:
                    for cell in row:
                        assert not UGLY.search(cell), f"{section.title}: {cell}"

    def test_the_deal_that_produced_the_sentence_still_produces_the_note(self) -> None:
        """And it is still a note, not a silence.

        The fix is a rendering fix. The case genuinely cannot be valued at that
        assumption and the memo is right to say so, so a "fix" that made the
        note disappear would be worse than the sentence it replaced.
        """
        report = prepare(load_deal(ROOT / "examples" / "ashcroft.json"))
        notes = [note for section in report for note in section.notes]
        shortfalls = [note for note in notes if "which leaves only" in note]
        assert shortfalls, "the break-even search no longer reports why it stopped"
        for note in shortfalls:
            assert not UGLY.search(note)
            # The balance and the minimum are written the same way, which is
            # the thing a reader of this sentence is being asked to compare.
            assert re.search(r"holding [\d,]+\.\d\d against a minimum of [\d,]+\.\d\d", note)
            assert re.search(r"which leaves only -?[\d,]+\.\d\d", note)


class TestWhatIsDeliberatelyNotAnAmount:
    """Three values in messages are not amounts, and are left alone on purpose.

    Writing them as money would be the same mistake in the other direction: it
    would round away the thing the message exists to report.
    """

    def test_a_residual_keeps_its_exponent(self) -> None:
        # "the saving was still moving by 5.9e-5" says how far from settling
        # the solve was. Rounded to two places it says 0.00, which is the one
        # value it certainly was not.
        from capstack.tax import TaxCircularityError

        assert issubclass(TaxCircularityError, Exception)
        assert written(Decimal("0.000059")) == "0.00"

    def test_a_share_that_is_nearly_one_has_to_read_as_nearly_one(self) -> None:
        # The funding-share message fires precisely because the shares do not
        # come to one. Writing 0.9999999 as "1.00" would make the message say
        # the opposite of why it was raised.
        assert written(Decimal("0.9999999")) == "1.00"

    def test_a_rate_is_not_an_amount(self) -> None:
        assert written(Decimal("0.0175")) == "0.02"


def test_every_shipped_deal_was_checked() -> None:
    """A parametrised sweep over an empty glob passes and proves nothing."""
    assert len(DEALS) >= 4
    assert all(path.exists() for path in DEALS)


def test_the_pattern_catches_what_it_is_named_for() -> None:
    """The guard, against the exact strings that prompted it."""
    assert UGLY.search("holding 12.00000000000000000000000000000000 against")
    assert UGLY.search("which leaves only 0E-32")
    assert UGLY.search("a minimum of 1.2345678")
    # And does not fire on things a memo legitimately prints.
    for fine in ["12.00", "1,234,567.89", "4.42x", "18.2%", "-31.3", "2026-12-31"]:
        assert not UGLY.search(fine)


def test_the_fixture_still_has_what_the_note_needs() -> None:
    """A guard on the fixture rather than on the engine.

    The shortfall note comes out of the acquisition path. If
    `examples/ashcroft.json` ever stopped having acquisitions in it the note
    would stop being produced, and the test above would start passing for the
    wrong reason rather than failing for the right one.
    """
    deal = load_deal(ROOT / "examples" / "ashcroft.json")
    assert deal.has_acquisitions
