"""The README's known gaps, and whether each one is still a gap.

A list of limitations is read as current whether or not it is, so the failure
mode is not an incomplete list but a stale one. Each line here is paired with a
predicate over the package's own public names and dataclass fields, and the
test fails three ways: a line with no predicate, a predicate with no line, and
a predicate that no longer holds because the thing it says is missing has been
built. Closing a gap therefore breaks this file, which is the point — the line
has to come out deliberately rather than be left behind.

The predicates are deliberately crude. They ask whether anything in the public
surface is *named* for the missing concept, which is a weaker question than
whether the concept is modelled, and weaker in the safe direction: a name can
appear without the behaviour, and then the line comes out and is replaced by
something sharper. What cannot happen is the behaviour appearing while the line
stays.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import pkgutil
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import capstack
from capstack.covenants import Measure
from capstack.sensitivity import Case
from capstack.tax import TaxPolicy

README = Path(__file__).resolve().parents[1] / "README.md"


def _names() -> frozenset[str]:
    """Every public name in the package, and every dataclass field on them.

    One inventory for every predicate below, so that a gap closed by adding a
    class, a function, a field or an enum member is caught the same way.
    """
    found: set[str] = set()
    for name in dir(capstack):
        if not name.startswith("_"):
            found.add(name)
    for info in pkgutil.iter_modules(capstack.__path__):
        module = importlib.import_module(f"capstack.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            found.add(name)
            if dataclasses.is_dataclass(value):
                found.update(f.name for f in dataclasses.fields(value))
            if isinstance(value, type) and issubclass(value, enum.Enum):
                found.update(value.__members__)
    return frozenset(found)


NAMES = _names()


def _nothing_named(pattern: str) -> bool:
    expression = re.compile(pattern, re.IGNORECASE)
    return not any(expression.search(name) for name in NAMES)


@dataclass(frozen=True, slots=True)
class Gap:
    #: The bolded lead of the README bullet, exactly as written.
    heading: str
    #: True while the gap is still a gap.
    still_open: Callable[[], bool]


GAPS: tuple[Gap, ...] = (
    Gap(
        "One tax rate, for the whole hold.",
        # A covenant threshold is a Driver, so it can tighten year by year. The
        # tax rate is a scalar on the policy, so a rate change part-way through
        # a hold cannot be described at all.
        lambda: "Driver" not in str(TaxPolicy.__dataclass_fields__["rate"].type),
    ),
    Gap(
        "No interest-rate hedge.",
        lambda: _nothing_named(r"hedge|collar|swaption")
        and "cap" not in {f.name for f in dataclasses.fields(capstack.Tranche)},
    ),
    Gap(
        "No equity cure.",
        lambda: _nothing_named(r"cure"),
    ),
    Gap(
        "One currency.",
        lambda: _nothing_named(r"currency|^fx$|exchange_rate"),
    ),
    Gap(
        "No minority interest in the target.",
        lambda: _nothing_named(r"minority|non_?controlling"),
    ),
    Gap(
        "The exit is one event for the whole stake.",
        lambda: _nothing_named(r"partial_exit|stake_sold|sell_?down|secondary_sale"),
    ),
    Gap(
        "A lease is an operating cost, not a liability in the stack.",
        lambda: _nothing_named(r"lease"),
    ),
    Gap(
        "Cash sweeps out of operating cash flow and nothing else.",
        lambda: _nothing_named(r"disposal|asset_sale|insurance_proceeds"),
    ),
    Gap(
        "Consideration is paid in full at close.",
        lambda: _nothing_named(r"earn_?out|deferred_consideration|escrow|holdback"),
    ),
    Gap(
        "Covenants are maintenance tests only.",
        lambda: _nothing_named(r"incurrence")
        and not any("INCURRENCE" in m.name for m in Measure),
    ),
    Gap(
        "Working capital is a rate on revenue, not a days build.",
        lambda: _nothing_named(r"^dso$|^dpo$|^dio$|days_sales|days_payable|days_inventory"),
    ),
    Gap(
        "The levered tax basis is opt-in.",
        # Worth 90 to 189 basis points of return on the shipped deals, and
        # still not the default, because flipping it moves every reported
        # figure including the hand-computed worked example.
        lambda: Case.run.__kwdefaults__ is not None
        and Case.run.__kwdefaults__.get("levered") is False,
    ),
)


def _readme_headings() -> list[str]:
    """The bolded lead of every bullet in the README's gaps section."""
    text = README.read_text(encoding="utf8")
    section = re.search(r"\n## Known gaps\n(.*?)(?=\n## )", text, re.DOTALL)
    assert section is not None, "the README has no known-gaps section"
    return re.findall(r"^- \*\*(.+?)\*\*", section.group(1), re.MULTILINE)


class TestTheKnownGapsList:
    def test_the_readme_lists_exactly_these_gaps_in_this_order(self) -> None:
        assert _readme_headings() == [gap.heading for gap in GAPS]

    def test_the_list_has_not_quietly_shrunk(self) -> None:
        assert len(GAPS) == 12

    def test_every_gap_is_still_a_gap(self) -> None:
        closed = [gap.heading for gap in GAPS if not gap.still_open()]
        assert closed == [], (
            "these gaps look closed; take the lines out of the README and the "
            f"entries out of this table: {closed}"
        )

    def test_the_inventory_the_predicates_read_is_not_empty(self) -> None:
        # A predicate of the form "nothing is named X" passes trivially against
        # an empty inventory, so the inventory itself is asserted: every module
        # walked, and names that are certainly there found.
        assert len(NAMES) > 400
        for certainly_there in ("Tranche", "undrawn_fee", "SweepGrid", "NET_LEVERAGE"):
            assert certainly_there in NAMES
        assert not _nothing_named(r"undrawn_fee")
