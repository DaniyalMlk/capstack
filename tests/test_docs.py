"""The site against the engine it describes.

The page is generated, so the usual failure -- a figure edited in the code and
not on the page -- cannot happen. The failures that *can* happen are the two
this file is about:

1. **The page quotes something no gate produced.** A figure that looks measured
   but was interpolated from somewhere else, or a row whose label drifted from
   the result beside it.
2. **The page stops quoting something.** A row dropped from the evidence table,
   a memo section that vanished, a grid that quietly stopped rebuilding.

So the assertions run in both directions: every row the gate produces appears
on the page, *and* the rendered rows are counted against the gate's, because a
forward-only check passes on an empty page.

What is deliberately not attempted is a value-direction scan of the page's free
text against the gate's figures. Two figures in a table of eight can be close
enough that "some number near here" matches the wrong one, which is the same
reasoning ``test_readme_evidence.py`` gives for the README. Every figure quoted
outside the table is matched inside the element it is rendered into instead --
``toContain``-style containment against the whole page is satisfied by the
evidence table, which is a hole a break sweep found in the sibling repository's
gate before this one was written.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))

from pages import (  # noqa: E402
    DISTRIBUTION,
    GRID_COLUMNS,
    GRID_ROWS,
    MEMO,
    MEMO_DEAL,
    PACKAGE,
    SOURCE,
    build_site,
    grid_table,
    measure,
)


@pytest.fixture(scope="module")
def measured() -> object:
    return measure()


@pytest.fixture(scope="module")
def files(measured: object) -> dict[str, str]:
    return build_site(measured)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def page(files: dict[str, str]) -> str:
    return files["index.html"]


@pytest.fixture(scope="module")
def memo(files: dict[str, str]) -> str:
    return files[MEMO]


# ------------------------------------------------------------ what it ships


def test_the_site_is_the_three_files_it_says_it_is(files: dict[str, str]) -> None:
    assert sorted(files) == ["index.html", MEMO, "style.css"]


def test_both_pages_link_the_stylesheet_and_fetch_nothing_else(
    page: str, memo: str, files: dict[str, str]
) -> None:
    for document in (page, memo):
        assert '<link rel="stylesheet" href="style.css">' in document
        # A documentation build that needs a CDN is a documentation build that
        # stops working, which is the same argument the engine makes for
        # having no runtime dependencies.
        assert not re.search(r"https?://[^\"' ]*\.(?:css|js)", document)
        assert "<script" not in document.lower()
        assert "@import" not in document
    assert len(files["style.css"]) > 3000


def test_the_landing_page_points_at_the_memo(page: str) -> None:
    assert f'href="{MEMO}"' in page


def test_the_distribution_and_the_import_are_both_named_and_are_not_the_same(
    page: str,
) -> None:
    # The split is load-bearing: `capstack` on the package index is an
    # unrelated project, so a page that quietly used one name for both would
    # send a reader to install somebody else's software.
    assert PACKAGE != DISTRIBUTION
    assert f"pip install {DISTRIBUTION}" in page
    assert f"<code>{DISTRIBUTION}</code>" in page
    assert f"<code>{PACKAGE}</code>" in page
    assert SOURCE in page


# -------------------------------------------------- the evidence table, both ways


def test_every_row_the_gate_produces_is_rendered_whole(
    page: str, measured: object
) -> None:
    # Label and result in one string. Asserting them separately passes on a
    # generator that paired the wrong result with a label.
    rows = measured.rows  # type: ignore[attr-defined]
    assert len(rows) >= 8
    for check, result in rows:
        rendered = (
            f"<tr><td>{_code(html.escape(check, quote=True))}</td>"
            f"<td>{_code(html.escape(result, quote=True))}</td></tr>"
        )
        assert rendered in page


def test_the_page_renders_no_rows_the_gate_did_not_produce(
    page: str, measured: object
) -> None:
    rows = measured.rows  # type: ignore[attr-defined]
    body = page.split('<table class="evidence">', 1)[1].split("</table>", 1)[0]
    assert body.count("<tr>") == len(rows) + 1  # the header is a row too
    assert body.count("<td>") == len(rows) * 2


def _code(text: str) -> str:
    out: list[str] = []
    for index, part in enumerate(text.split("`")):
        out.append(part if index % 2 == 0 else f"<code>{part}</code>")
    return "".join(out)


# --------------------------------------- figures quoted outside the table


def test_the_funding_gap_headline_is_the_gate_s_own_figure(
    page: str, measured: object
) -> None:
    figures = measured.figures  # type: ignore[attr-defined]
    gap = "0" if figures.funding_worst_gap == 0 else str(figures.funding_worst_gap)
    assert f"<strong>{gap}</strong>" in page
    # The claim is that it is nil. Quoting it as "small" would be quoting a
    # weaker claim than the one exact arithmetic actually supports.
    assert figures.funding_worst_gap == 0
    assert "not within a tolerance" in page


def test_the_quadratic_headline_is_the_measured_error(
    page: str, measured: object
) -> None:
    figures = measured.figures  # type: ignore[attr-defined]
    assert f"<strong>{figures.ambiguous_worst_error:.1e}</strong>" in page
    # And the thing it is an error *against* is on the page as the exact
    # numbers, because that is the whole reason this row is worth more than
    # the self-consistency rows.
    assert "exactly 20% and 30%" in page
    assert figures.ambiguous_worst_error < 1e-10


def test_the_deduction_headline_is_the_measured_range(
    page: str, measured: object
) -> None:
    figures = measured.figures  # type: ignore[attr-defined]
    low = f"{figures.return_gain_low:.0f}"
    high = f"{figures.return_gain_high:.0f}"
    assert f"<strong>{low}&ndash;{high}<span" in page
    assert figures.return_gain_high > figures.return_gain_low


def test_the_section_count_is_the_memo_s_own(page: str, measured: object) -> None:
    report = measured.report  # type: ignore[attr-defined]
    assert f"it runs to {len(report)} sections" in page
    assert len(report) >= 6


def test_the_cell_count_is_the_grid_s_own(page: str, measured: object) -> None:
    grid = measured.grid  # type: ignore[attr-defined]
    cells = sum(len(row) for row in grid.cells)
    assert cells == len(GRID_ROWS) * len(GRID_COLUMNS)
    assert f"This one is {cells} complete rebuilds" in page


# ------------------------------------------------------------------ the memo


def test_the_memo_renders_every_section_the_engine_produced(
    memo: str, measured: object
) -> None:
    report = measured.report  # type: ignore[attr-defined]
    assert memo.count('<section class="memo__section">') == len(report)
    for section in report:
        assert f"<h2>{html.escape(section.title, quote=True)}</h2>" in memo


def test_every_memo_figure_is_the_string_the_engine_formatted(
    memo: str, measured: object
) -> None:
    """Not reformatted here.

    Formatting happens where a section is assembled, which is what stops the
    terminal and this page rounding the same figure two different ways. So the
    page has to carry the engine's strings verbatim, and this is the assertion
    that says so -- every labelled line and every table cell, as the engine
    wrote it.
    """
    report = measured.report  # type: ignore[attr-defined]
    seen = 0
    for section in report:
        for line in section.lines:
            assert (
                f'<th scope="row">{html.escape(line.label, quote=True)}</th>'
                f'<td class="num">{html.escape(line.value, quote=True)}</td>'
            ) in memo
            seen += 1
        if section.table is not None:
            for row in section.table.rows:
                for cell in row:
                    assert f">{html.escape(cell, quote=True)}</td>" in memo
                    seen += 1
    assert seen > 50


def test_the_memo_names_the_deal_file_it_was_printed_from(
    memo: str, measured: object
) -> None:
    report = measured.report  # type: ignore[attr-defined]
    assert f"examples/{MEMO_DEAL}" in memo
    assert html.escape(report.name, quote=True) in memo


# ------------------------------------------------------------------ the grid


def test_every_grid_cell_prints_its_own_figure(page: str, measured: object) -> None:
    from capstack.sensitivity import format_value

    grid = measured.grid  # type: ignore[attr-defined]
    for row in grid.cells:
        for cell in row:
            if cell.value is None:
                continue
            assert format_value(grid.metric.unit, cell.value) in page


def test_the_shade_carries_rank_and_the_extremes_are_the_extremes(
    page: str, measured: object
) -> None:
    """The colour is allowed to say one thing, and this is that it says it.

    A heat map whose weights do not span the range is a heat map that is
    lying about which cell is the worst. Both ends have to be present, and
    an equality between the two would mean every cell shaded identically --
    which happens, legitimately, on a grid whose metric does not move, so
    the spread of the underlying values is asserted beside it rather than
    the weights alone.
    """
    grid = measured.grid  # type: ignore[attr-defined]
    values = [c.value for row in grid for c in row if c.value is not None]
    assert len(values) == len(GRID_ROWS) * len(GRID_COLUMNS)
    assert max(values) > min(values)
    weights = sorted(float(w) for w in re.findall(r"--w:([0-9.]+)", page))
    assert len(weights) == len(values)
    assert weights[0] == pytest.approx(0.0, abs=1e-9)
    assert weights[-1] == pytest.approx(1.0, abs=1e-3)


def test_the_published_grid_reaches_every_cell(page: str, measured: object) -> None:
    grid = measured.grid  # type: ignore[attr-defined]
    unreachable = sum(1 for row in grid for cell in row if cell.value is None)
    breached = len(grid.breaches)
    assert (unreachable, breached) == (0, 0)
    assert page.count('class="num out"') == 0
    assert page.count("num breach") == 0


def test_an_unreachable_cell_is_marked_and_a_breach_is_flagged() -> None:
    """Both of those paths, on a grid that actually has them.

    This started as an assertion over the *published* grid, which reaches
    every cell and breaches nothing -- so both sides were zero and the check
    could not fail. A break sweep said so: removing the marking entirely
    changed no output and passed. An assertion whose code path no input
    reaches is not an assertion, so the grid is constructed here.

    A blank cell in a sensitivity table reads as zero, which on a return is a
    very different statement from "the engine could not value this at all",
    and a cell whose covenant tripped is a return nobody could have earned.
    """
    from capstack import Axis, Cell, Dimension, Grid, Metric, money

    grid = Grid(
        rows=Axis.of(Dimension.EXIT_MULTIPLE, ["8", "9"]),
        columns=Axis.of(Dimension.LEVERAGE, ["4", "5"]),
        metric=Metric.IRR,
        cells=(
            (
                Cell(row=money(8), column=money(4), value=money("0.10")),
                Cell(row=money(8), column=money(5), value=None, note="no exit"),
            ),
            (
                Cell(row=money(9), column=money(4), value=money("0.20")),
                Cell(
                    row=money(9),
                    column=money(5),
                    value=money("0.30"),
                    breached=True,
                    breach_note="net leverage",
                ),
            ),
        ),
        base=None,
    )
    rendered = grid_table(grid)
    assert rendered.count('class="num out"') == 1
    assert "&mdash;" in rendered
    assert rendered.count('class="num breach"') == 1
    # The weights still span the reachable range and ignore the hole.
    weights = sorted(float(w) for w in re.findall(r"--w:([0-9.]+)", rendered))
    assert weights == [0.0, 0.5, 1.0]
