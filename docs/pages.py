"""The documentation site, generated from the engine rather than written beside it.

Three rules decide what is allowed on the page.

**Every figure comes from code that runs.** The evidence table is rendered from
``examples/evidence.py`` -- the same gate CI runs and the same rows the README
carries, which exits non-zero when a figure moves -- and the memo and the grid
are printed by the engine at build time. Nothing here is a number somebody
typed, because a number typed on a page is a number that will be wrong in a
month. ``tests/test_docs.py`` fails in both directions if that stops being
true.

**The memo is the memo.** What this engine produces is an investment committee
paper, and the honest way to show that is to print one. ``report.prepare``
already returns the thing structured -- sections, labelled lines, tables with
their own alignment -- so the page renders those rather than wrapping the
command line's text in a ``<pre>``, and the figures in it are formatted once,
where the section is assembled, exactly as they are on a terminal.

**The grid is a rebuild, not a gradient.** Every cell of the sensitivity table
on the page is a full rebuild of the deal: the structure is re-levered, the
schedule re-run, the covenants re-tested. That is the expensive choice and the
one worth showing, so the page says the cell count and the gate checks the base
cell against the deal run on its own.

The site is three files, and ``site/`` is gitignored: the repository holds this
generator, not its output.
"""

from __future__ import annotations

import html
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

from capstack import Axis, Dimension, Grid, Metric, Money, load_deal
from capstack.report import Report, Section, prepare
from capstack.sensitivity import format_value

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "https://github.com/DaniyalMlk/capstack"

#: The index name, which is not the import name: ``capstack`` on the package
#: index is an unrelated project.
DISTRIBUTION = "capstack-lbo"
PACKAGE = "capstack"

#: The deal the published memo is for. Ashcroft is the one with something to
#: say in every section -- it has acquisitions during the hold, a covenant that
#: gets tight, and debt that accrues in kind -- so a reader sees the whole memo
#: rather than a memo with three sections missing.
MEMO_DEAL = "ashcroft.json"

#: Where the memo is published relative to the landing page.
MEMO = "memo.html"


def gate() -> ModuleType:
    """Import ``examples/evidence.py``, which is a script and not a package.

    The same loader ``tests/test_readme_evidence.py`` uses, for the same
    reason: the gate has to be the gate the README quotes and CI runs, not a
    second copy of its arithmetic that can drift from it.
    """
    path = ROOT / "examples" / "evidence.py"
    spec = importlib.util.spec_from_file_location("capstack_evidence", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError(f"cannot load the evidence gate from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Figures(Protocol):
    """The part of the gate's measurement this page depends on.

    Written out rather than imported, because ``examples/evidence.py`` is a
    script and is loaded at run time -- so there is no type to import. Stating
    the five fields here is the better half of that trade anyway: it is an
    explicit list of what the front page is allowed to quote, and mypy checks
    the page against it.
    """

    deals: int
    funding_worst_gap: Money
    ambiguous_worst_error: float
    return_gain_low: float
    return_gain_high: float


@dataclass(frozen=True)
class Measured:
    """Everything the page is built from, measured once."""

    figures: Figures
    rows: tuple[tuple[str, str], ...]
    report: Report
    grid: Grid


#: The axes the published grid is run over. Exit multiple down and opening
#: leverage across is the pair an investment committee actually argues about,
#: and the range is wide enough that the covenant flags something.
GRID_ROWS: tuple[str, ...] = ("8", "9", "10", "11")
GRID_COLUMNS: tuple[str, ...] = ("4", "5", "6")


def measure() -> Measured:
    """Run the gate, the memo and the grid."""
    evidence = gate()
    figures = evidence.measure()
    evidence.check(figures)

    deal = load_deal(ROOT / "examples" / MEMO_DEAL)
    memo = prepare(deal)

    grid_deal = load_deal(ROOT / "examples" / "meridian.json")
    grid = Grid.run(
        grid_deal,
        rows=Axis.of(Dimension.EXIT_MULTIPLE, GRID_ROWS),
        columns=Axis.of(Dimension.LEVERAGE, GRID_COLUMNS),
        metric=Metric.IRR,
    )
    return Measured(figures=figures, rows=tuple(evidence.rows(figures)), report=memo, grid=grid)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def _shell(title: str, body: str, description: str, *, home: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' \
viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%231b2a41'/%3E\
%3Crect x='3' y='9.5' width='10' height='3' fill='%23c9a227'/%3E\
%3Crect x='3' y='6' width='10' height='2.6' fill='white' opacity='0.78'/%3E\
%3Crect x='3' y='3.3' width='10' height='2' fill='white' opacity='0.45'/%3E%3C/svg%3E">
<link rel="stylesheet" href="{esc(home)}style.css">
</head>
<body>
{body}
</body>
</html>
"""


def _masthead(home: str, *, here: str) -> str:
    links = [
        ("#stack", "The stack"),
        (f"{home}{MEMO}", "The memo"),
        ("#evidence", "What is checked"),
        ("#install", "Install"),
        (SOURCE, "Source"),
    ]
    # Concatenated rather than one f-string: a backslash inside an f-string
    # expression is a syntax error before 3.12, and the floor here is 3.11.
    current = ' aria-current="page"'
    rendered = "\n    ".join(
        '<a href="' + esc(href) + '"' + (current if href == here else "")
        + ">" + esc(label) + "</a>"
        for href, label in links
    )
    return f"""<header class="masthead col--wide">
  <a class="mark" href="{esc(home or "./")}"><span class="mark__glyph"
    aria-hidden="true"></span>capstack</a>
  <nav class="masthead__nav">
    {rendered}
  </nav>
</header>"""


# ------------------------------------------------------------------ the memo


def _section_html(section: Section) -> str:
    parts = [f"<h2>{esc(section.title)}</h2>"]
    if section.summary:
        parts.append(f'<p class="memo__summary">{esc(section.summary)}</p>')
    if section.lines:
        rows = "\n".join(
            f"<tr><th scope=\"row\">{esc(line.label)}</th>"
            f'<td class="num">{esc(line.value)}</td>'
            f'<td class="gloss">{esc(line.note)}</td></tr>'
            for line in section.lines
        )
        parts.append(f'<table class="memo__lines">\n<tbody>\n{rows}\n</tbody>\n</table>')
    if section.table is not None:
        table = section.table
        alignment = table.alignment
        head = "".join(
            f'<th class="{"num" if align == "r" else ""}">{esc(heading)}</th>'
            for heading, align in zip(table.headings, alignment, strict=True)
        )
        body = "\n".join(
            "<tr>"
            + "".join(
                f'<td class="{"num" if align == "r" else ""}">{esc(cell)}</td>'
                for cell, align in zip(row, alignment, strict=True)
            )
            + "</tr>"
            for row in table.rows
        )
        parts.append(
            f'<div class="memo__scroll"><table class="memo__grid">\n'
            f"<thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody>\n</table></div>"
        )
    for note in section.notes:
        parts.append(f'<p class="memo__note">{esc(note)}</p>')
    return '<section class="memo__section">\n' + "\n".join(parts) + "\n</section>"


def memo_page(report: Report) -> str:
    """The whole memo, as HTML.

    Rendered from the ``Report`` rather than from the command line's text. The
    figures are already strings by the time they get here -- formatting happens
    when a section is assembled, which is what stops the terminal and this page
    rounding the same number two different ways.
    """
    close = "" if report.close is None else f" &middot; close {report.close.isoformat()}"
    sections = "\n".join(_section_html(section) for section in report)
    body = f"""{_masthead("", here=MEMO)}

<main class="memo col--wide">
  <p class="memo__back"><a href="./">&larr; capstack</a></p>
  <h1>{esc(report.name)}</h1>
  <p class="memo__standfirst">Investment committee{close}. Printed by
  <code>capstack report examples/{esc(MEMO_DEAL)}</code> when this page was
  built, from the deal file in the repository. Every figure below was computed
  for this page; none of it was written here.</p>
{sections}
</main>

<footer class="foot col--wide">
  <p>capstack, published as <code>{esc(DISTRIBUTION)}</code>.
  <a href="{esc(SOURCE)}">Source</a>.</p>
</footer>"""
    return _shell(
        f"{report.name} &middot; capstack",
        body,
        f"The investment committee memo capstack prints for {report.name}: the "
        "funding table, the operating case, the debt schedule, the covenants "
        "and the exit bridge.",
        home="",
    )


# ------------------------------------------------------------- the front page


def _evidence_table(rows: tuple[tuple[str, str], ...]) -> str:
    body = "\n".join(
        f"<tr><td>{_code(esc(check))}</td><td>{_code(esc(result))}</td></tr>"
        for check, result in rows
    )
    return (
        '<table class="evidence">\n<thead><tr><th>Check</th><th>Result</th></tr></thead>\n'
        f"<tbody>\n{body}\n</tbody>\n</table>"
    )


def _code(text: str) -> str:
    """Backticks to ``<code>``, after escaping."""
    out: list[str] = []
    for index, part in enumerate(text.split("`")):
        out.append(part if index % 2 == 0 else f"<code>{part}</code>")
    return "".join(out)


def grid_table(grid: Grid) -> str:
    """The sensitivity grid, shaded by where each cell sits in its own range.

    The shade carries one thing -- rank within the measured range -- and the
    figure carries the value, which is why the number is printed in every cell
    rather than left to a colour scale to imply. Cells the engine could not
    reach are marked rather than left blank, because a blank cell in a
    sensitivity table reads as zero.
    """
    values = [cell.value for row in grid for cell in row if cell.value is not None]
    if not values:
        raise RuntimeError("the published grid has no reachable cells")
    low, high = min(values), max(values)
    span = high - low

    head = "".join(
        f'<th class="num">{esc(grid.columns.format(value))}</th>'
        for value in grid.columns
    )
    lines: list[str] = []
    for label, row in zip(grid.rows, grid.cells, strict=True):
        cells: list[str] = []
        for cell in row:
            if cell.value is None:
                cells.append('<td class="num out">&mdash;</td>')
                continue
            weight = 0.0 if span == 0 else float((cell.value - low) / span)
            flag = " breach" if cell.breached else ""
            cells.append(
                f'<td class="num{flag}" style="--w:{weight:.3f}">'
                f"{esc(format_value(grid.metric.unit, cell.value))}</td>"
            )
        lines.append(
            f'<tr><th scope="row">{esc(grid.rows.format(label))}</th>'
            + "".join(cells)
            + "</tr>"
        )
    return (
        '<div class="memo__scroll"><table class="grid">\n'
        f'<thead><tr><th scope="col">exit &times; / leverage &rarr;</th>{head}</tr></thead>\n'
        "<tbody>\n" + "\n".join(lines) + "\n</tbody>\n</table></div>"
    )


def landing(measured: Measured) -> str:
    figures = measured.figures
    bps_high = f"{figures.return_gain_high:.0f}"
    report = measured.report
    grid = measured.grid
    cells = sum(len(row) for row in grid.cells)
    sections = len(report)

    body = f"""{_masthead("", here="./")}

<main>

<section class="lede col--wide">
  <h1>A funding table that nearly balances has a missing&nbsp;line.</h1>
  <p class="standfirst">A leveraged buyout engine in Python, with no runtime
  dependencies. Money is <code>Decimal</code> at thirty-four digits, so the
  identities a model is supposed to satisfy are checked as <em>equalities</em>
  rather than against a tolerance &mdash; and a tolerance is exactly where the
  error these checks exist to catch would hide. It reads a deal file and prints
  the memo: the funding table, the operating case, the debt schedule, the
  covenants, the exit and the bridge that explains it.</p>
  <p class="actions">
    <a class="cta" href="{esc(MEMO)}">Read a memo it printed</a>
    <a class="plain" href="{esc(SOURCE)}">Read the source</a>
  </p>
</section>

<section class="claim">
  <div class="claim__inner col--wide">
    <div class="claim__figures">
      <p class="claim__figure"><strong>{esc(_exact(figures.funding_worst_gap))}</strong>
        <span>worst gap on the funding table across {figures.deals} shipped deals,
        by equality and not within a tolerance</span></p>
      <p class="claim__figure"><strong>{figures.ambiguous_worst_error:.1e}</strong>
        <span>from the quadratic&rsquo;s exact roots, on a stream with two
        internal rates of return</span></p>
      <p class="claim__figure"><strong>{figures.return_gain_low:.0f}&ndash;{bps_high}<span
        class="unit">bps</span></strong>
        <span>of return the interest deduction is worth, measured rather than
        assumed</span></p>
    </div>
    <p class="claim__detail">The second one is the only kind of check worth
    much. A flow of &minus;1,000 / +2,500 / &minus;1,560 is
    <code>1000x&sup2; &minus; 2500x + 1560 = 0</code> in
    <code>x = 1 + r</code>; the discriminant is 10,000, a perfect square, so
    the roots are exactly 1.2 and 1.3 and the rates are exactly 20% and 30%.
    The solver knows nothing about quadratics &mdash; it scans for sign changes
    and refines every bracket it finds. Agreeing with the algebra is a
    statement about the solver rather than a restatement of it.</p>
  </div>
</section>

<section id="stack" class="stack col--wide">
  <h2>The memo, printed rather than described</h2>
  <p>What this produces is an investment committee paper, so here is one. It
  was printed when this page was built, from a deal file in the repository, and
  it runs to {sections} sections: the transaction and its funding table, the
  operating case period by period, the debt schedule with the sweep and the
  revolver, the covenants and their headroom, the exit, the acquisitions made
  during the hold, and the bridge that says where the equity value came
  from.</p>
  <p class="actions">
    <a class="cta" href="{esc(MEMO)}">Open the memo</a>
  </p>

  <h2 class="stack__second">Every cell is a rebuild</h2>
  <p>A sensitivity table is usually a gradient someone drew around a base case.
  This one is {cells} complete rebuilds of the deal: for every pair of
  assumptions the structure is re-levered, the schedule is re-run, the
  covenants are re-tested and the exit is re-priced. That is the expensive
  choice, and it is the reason a cell can come back saying the covenant
  breached rather than quietly reporting a return nobody could have earned.
  The gate checks the base cell against the deal run on its own and the two
  agree by equality.</p>
  {grid_table(grid)}
  <p class="caption">Internal rate of return on Project Meridian, exit multiple
  down and opening leverage across. The shade is rank within the range
  measured, nothing more; the figure in each cell is the value.</p>
</section>

<section id="evidence" class="evidence-section col--wide">
  <h2>What is checked, and against what</h2>
  <p>An LBO model is easy to describe and hard to believe: every one of them
  claims to price a deal, schedule its debt and report a return, and the
  question a reader actually has is what any of it was checked against. Every
  figure in this table is produced by <code>examples/evidence.py</code>, which
  runs in CI and exits non-zero if one of them moves &mdash; and it is the same
  gate that renders this page, so the page cannot drift from the engine
  either.</p>
  {_evidence_table(measured.rows)}
  <p class="aside"><strong>Three of these rows say zero, and they mean
  it.</strong> Sources and uses, the value bridge and the base cell of a grid
  are exact-arithmetic identities: there is no rounding left over to allow for,
  so quoting them as &ldquo;small&rdquo; would be quoting a weaker claim than
  the one that holds. The rows that <em>are</em> bounds are the ones with a
  root-finder or a floating-point rate underneath them, and those are written
  as bounds.</p>
  <p class="aside"><strong>A funding table out by a few thousand on a billion
  has not nearly balanced.</strong> It has a missing line. The same is true of
  a bridge that nearly ties to the change in equity value, and of a schedule
  that nearly amortises to zero. All three are asserted as equality on exact
  arithmetic, which is possible only because money here is never a float
  &mdash; and that is the whole argument for paying what
  <code>Decimal</code> costs.</p>
</section>

<section id="install" class="install col--wide">
  <h2>The first minute</h2>
  <pre><code>pip install {esc(DISTRIBUTION)}</code></pre>
  <pre><code>capstack report deal.json          # the whole memo
capstack sensitivity deal.json \\
    --rows exit-multiple:8,9,10,11 \\
    --columns leverage:4,5,6 --metric irr</code></pre>
  <p><strong>The distribution is <code>{esc(DISTRIBUTION)}</code> and the
  import is <code>{esc(PACKAGE)}</code>.</strong> <code>capstack</code> on the
  package index is an unrelated project, so the names differ; both
  <code>capstack</code> and <code>{esc(DISTRIBUTION)}</code> work as commands,
  so <code>uvx {esc(DISTRIBUTION)}</code> resolves. Python 3.11 or newer, no
  runtime dependencies, and the deal files in
  <code>examples/</code> are real enough to run.</p>
  <pre><code>from capstack import load_deal
from capstack.report import prepare

memo = prepare(load_deal("examples/ashcroft.json"))</code></pre>
</section>

<section class="covers col--wide">
  <h2>What is in it</h2>
  <p>An operating model with drivers that can be stated as growth, as margin or
  as an absolute, and a period grid that handles a stub at close rather than
  pretending a November signing is a January one. A capital structure of fixed
  and floating tranches with amortisation, cash sweeps, a revolver, payment in
  kind, commitment fees on undrawn facilities and delayed draw. Interest
  deductibility solved against the debt it is charged on, because the deduction
  depends on the interest and the interest depends on the cash the deduction
  leaves &mdash; a fixed point, not a formula. Maintenance covenants with
  headroom and the first breach located. Acquisitions during the hold and the
  entry multiple they blend to. Returns by security, with the management
  incentive pool taken out of the right pocket. Break-evens found by solving
  rather than by stepping, and sensitivity grids where each cell is the whole
  engine run again.</p>
  <p>The reasoning is in the source rather than in a wiki. Every module says
  which convention it implements and why that one rather than the alternative,
  which is the part of a financial model that is actually hard &mdash; and
  <code>ROADMAP.md</code> says what is not modelled, which is the other
  part.</p>
</section>

</main>

<footer class="foot col--wide">
  <p>capstack, published as <code>{esc(DISTRIBUTION)}</code>. Built from the
  engine&rsquo;s own evidence gate, so no figure on this page was typed by
  hand. <a href="{esc(SOURCE)}">Source</a>.</p>
</footer>"""
    return _shell(
        "capstack &middot; a leveraged buyout engine",
        body,
        "capstack is a leveraged buyout engine in Python: exact Decimal money, "
        "a debt schedule solved against its own interest deduction, and "
        "sensitivity grids where every cell is a full rebuild of the deal.",
        home="",
    )


def _exact(amount: Money) -> str:
    """A gap, rendered the way the gate renders it."""
    return "0" if amount == 0 else f"{amount}"


STYLE = """/*
 * Generated by docs/site.py.
 *
 * The palette is chosen for the only thing on these pages that matters, which
 * is a column of money: a warm paper ground, hairlines instead of shadows, ink
 * that is navy rather than black, and one gold accent used where a figure is
 * the point. Figures are tabular everywhere. A column of money whose digits do
 * not line up is the fastest way to make a financial page look wrong to the
 * only people qualified to read it.
 */

:root {
  color-scheme: light dark;

  --paper: #fbfaf7;
  --surface: #ffffff;
  --sunken: #f3f1ec;
  --rule: #e4e1d8;
  --rule-strong: #cfcabd;

  --ink: #1b2a41;
  --ink-soft: #4c5668;
  --ink-mute: #767e8d;

  --accent: #1b2a41;
  --gold: #8a6d12;
  --gold-soft: rgba(201, 162, 39, 0.34);
  --bad: #94302c;
  --bad-soft: #f8eae9;

  --font: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;

  --measure: 38rem;
  --wide: 64rem;
  --gutter: 1.25rem;
  --rail: max(var(--gutter), 50% - var(--wide) / 2);
}

@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    --paper: #14161a;
    --surface: #1c1f25;
    --sunken: #22262d;
    --rule: #2e333b;
    --rule-strong: #434a55;
    --ink: #eceef2;
    --ink-soft: #b4bac6;
    --ink-mute: #8b919d;
    --accent: #9fc0ea;
    --gold: #d9b43f;
    --gold-soft: rgba(217, 180, 63, 0.30);
    --bad: #e08e8a;
    --bad-soft: #35211f;
  }
}

.col--wide { padding-inline: var(--rail); }

/*
 * Prose is capped at the measure by a child selector, never on the element
 * carrying the rail: with border-box sizing the padding would eat the
 * max-width and leave the text a few words across.
 */
.lede > *, .stack > p, .install > *, .covers > *, .foot > p,
.evidence-section > p, .memo > p { max-width: var(--measure); }
.lede h1 { max-width: 21ch; }

*, *::before, *::after { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font);
  font-size: 1.0625rem;
  line-height: 1.62;
  -webkit-font-smoothing: antialiased;
}

a { color: var(--accent); text-underline-offset: 2px; }
a:hover { color: var(--gold); }

code, pre, .num, .claim__figure strong, .evidence td, .evidence th,
.grid, .memo__lines td, .memo__grid { font-family: var(--mono); }

.num, .claim__figure strong, .evidence td, .grid td, .memo__lines .num,
.memo__grid td { font-variant-numeric: tabular-nums; }

code { font-size: 0.9em; }

pre {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 7px;
  padding: 0.9rem 1.05rem;
  overflow-x: auto;
  font-size: 0.8125rem;
  line-height: 1.6;
  margin: 1.05rem 0;
  max-width: var(--measure);
}

pre code { font-size: inherit; }

h1, h2 { letter-spacing: -0.02em; font-weight: 640; text-wrap: balance; }

/* -- masthead ------------------------------------------------------------ */

.masthead {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.5rem;
  align-items: baseline;
  justify-content: space-between;
  padding-block: 1.35rem;
  border-bottom: 1px solid var(--rule);
}

.mark {
  font-weight: 650;
  font-size: 1.0625rem;
  color: var(--ink);
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 0.55rem;
}

.mark__glyph {
  width: 0.9rem;
  height: 0.9rem;
  border-radius: 2px;
  background: linear-gradient(var(--ink) 0 55%, var(--gold) 55% 100%);
  flex: none;
}

.masthead__nav { display: flex; flex-wrap: wrap; gap: 0.35rem 1.2rem; font-size: 0.9375rem; }
.masthead__nav a { color: var(--ink-soft); text-decoration: none; }
.masthead__nav a:hover, .masthead__nav a[aria-current] { color: var(--gold); }

/* -- lede ---------------------------------------------------------------- */

.lede { padding-block: 4rem 2.4rem; }

h1 { margin: 0 0 1.05rem; font-size: clamp(2rem, 6vw, 3rem); line-height: 1.08; }

.standfirst { margin: 0; font-size: 1.125rem; color: var(--ink-soft); }

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.7rem 1.2rem;
  align-items: center;
  margin: 1.8rem 0 0;
}

.cta {
  background: var(--ink);
  color: var(--paper);
  padding: 0.58rem 1.1rem;
  border-radius: 6px;
  font-size: 0.9375rem;
  font-weight: 550;
  text-decoration: none;
}

.cta:hover { background: var(--gold); color: var(--paper); }

.plain { font-size: 0.9375rem; color: var(--ink-soft); }

/* -- the claim ----------------------------------------------------------- */

.claim { background: var(--sunken); border-block: 1px solid var(--rule); padding-block: 2.5rem; }

.claim__figures {
  display: grid;
  gap: 1.5rem 2.4rem;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  margin-bottom: 1.7rem;
}

.claim__figure { margin: 0; display: flex; flex-direction: column; gap: 0.3rem; }

.claim__figure strong {
  font-size: clamp(1.7rem, 4.2vw, 2.3rem);
  line-height: 1;
  font-weight: 600;
  letter-spacing: -0.03em;
  color: var(--gold);
}

.claim__figure .unit { font-size: 0.45em; margin-left: 0.25em; letter-spacing: 0; }
.claim__figure span { font-size: 0.875rem; color: var(--ink-soft); line-height: 1.45; }
.claim__detail { margin: 0; max-width: 42rem; font-size: 0.9375rem; color: var(--ink-soft); }

/* -- sections ------------------------------------------------------------ */

.stack, .evidence-section { padding-block: 3.2rem; }
.install { padding-block: 1rem 2rem; }
.covers { padding-block: 1rem 3.2rem; }

h2 { margin: 0 0 1rem; font-size: 1.45rem; }
.stack__second { margin-top: 3rem; }
.stack p, .evidence-section p, .install p, .covers p, .memo p { margin: 0 0 1rem; }

.caption { font-size: 0.8125rem; color: var(--ink-mute); max-width: var(--measure); }

.aside {
  font-size: 0.9375rem;
  color: var(--ink-soft);
  border-left: 2px solid var(--rule-strong);
  padding-left: 0.9rem;
}

.aside strong { color: var(--ink); font-weight: 600; }

/* -- tables -------------------------------------------------------------- */

.memo__scroll { overflow-x: auto; margin: 1.3rem 0; }

.evidence, .grid, .memo__grid, .memo__lines {
  border-collapse: collapse;
  width: 100%;
  background: var(--surface);
  font-size: 0.8125rem;
}

.evidence { border: 1px solid var(--rule); margin: 1.4rem 0; }

.evidence th, .evidence td, .grid th, .grid td,
.memo__grid th, .memo__grid td, .memo__lines th, .memo__lines td {
  text-align: left;
  vertical-align: top;
  padding: 0.55rem 0.8rem;
  border-bottom: 1px solid var(--rule);
  line-height: 1.5;
}

.evidence thead th, .grid thead th, .memo__grid thead th {
  background: var(--sunken);
  font-weight: 600;
  color: var(--ink-soft);
  white-space: nowrap;
  font-size: 0.75rem;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.evidence tbody tr:last-child td, .grid tbody tr:last-child td,
.memo__grid tbody tr:last-child td, .memo__lines tr:last-child td { border-bottom: 0; }

.evidence td:first-child { color: var(--ink-soft); width: 42%; min-width: 15rem; }
.evidence code, .grid code { font-size: 1em; }

.num, td.num, th.num { text-align: right; white-space: nowrap; }

/*
 * The grid: shade carries rank, the figure carries the value.
 *
 * Capped well short of the rail. Four columns stretched across 64rem puts the
 * row label and the figure it belongs to at opposite ends of the screen, which
 * is the one thing a table of money must not do.
 */
.grid { border: 1px solid var(--rule); max-width: 34rem; }

/*
 * The column headers here are values, not words, so they are not upper-cased:
 * a multiple is written `4.00x` and `4.00X` is a different notation. The row
 * labels below them were already lower case, which made the disagreement
 * visible the moment the page was looked at.
 */
.grid thead th { text-transform: none; font-size: 0.8125rem; letter-spacing: 0; }
.grid tbody th { font-weight: 550; white-space: nowrap; color: var(--ink-soft); }
.grid tbody td {
  background: color-mix(in oklab, var(--gold-soft) calc(var(--w) * 100%), transparent);
}
.grid td.breach { color: var(--bad); background: var(--bad-soft); font-weight: 600; }
.grid td.out { color: var(--ink-mute); }

/* -- the memo ------------------------------------------------------------ */

.memo { padding-block: 2.6rem 3.4rem; }
.memo__back { font-size: 0.875rem; margin: 0 0 1.4rem; }
.memo__standfirst { color: var(--ink-soft); font-size: 1rem; }
.memo__section { margin-top: 2.6rem; }
.memo__section h2 { font-size: 1.2rem; margin-bottom: 0.7rem; }
.memo__summary { color: var(--ink-soft); max-width: var(--measure); }
.memo__note { font-size: 0.875rem; color: var(--ink-mute); max-width: var(--measure); }

.memo__lines {
  border: 1px solid var(--rule);
  margin: 1.2rem 0;
  max-width: 46rem;
  font-size: 0.8125rem;
}

.memo__lines th { font-weight: 500; color: var(--ink-soft); width: 45%; }
.memo__lines .gloss {
  font-family: var(--font);
  color: var(--ink-mute);
  font-size: 0.8125rem;
  width: 35%;
}

.memo__grid { border: 1px solid var(--rule); }

/* -- footer -------------------------------------------------------------- */

.foot {
  border-top: 1px solid var(--rule);
  padding-block: 1.8rem 2.5rem;
  font-size: 0.875rem;
  color: var(--ink-mute);
}

.foot p { margin: 0; }

@media (max-width: 46rem) {
  .evidence { display: block; overflow-x: auto; }
  .memo__lines .gloss { display: none; }
  .memo__lines th { width: auto; }
}
"""


def build_site(measured: Measured | None = None) -> dict[str, str]:
    """Every file the site is made of, keyed by the name it is published under.

    Pure: it reads the engine and returns strings, so ``tests/test_docs.py``
    can call it without a filesystem and ``docs/build.py`` is the only thing
    that writes anything.
    """
    taken = measure() if measured is None else measured
    return {
        "index.html": landing(taken),
        MEMO: memo_page(taken.report),
        "style.css": STYLE,
    }
