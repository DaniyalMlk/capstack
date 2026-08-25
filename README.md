# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: seven layers are in — the numerics (exact money, day counts, period grids,
the return measures), the transaction (entry valuation, a sources and uses table
that balances, and the opening balance sheet after the recapitalisation), the
operating case (drivers through to unlevered free cash flow), the capital
structure (interest, amortisation, a cash sweep by seniority and a revolver that
runs both ways), the covenants (maintenance tests, headroom measured in EBITDA,
and a sweep that steps with a leverage grid), and the exit (equity value,
returns by security through a preferred waterfall, and a value-creation bridge),
and the analysis on top of them (two-dimensional sensitivity, with the whole
engine rebuilt and re-run at every cell). The investment committee report is
next; see [ROADMAP.md](ROADMAP.md).

## Install

```bash
python -m pip install -e ".[dev]"
```

## Use

```console
$ capstack returns 2026-06-30:-420000000:"sponsor equity" \
                   2031-06-30:1134000000:"exit proceeds"
Cash flows
  2026-06-30  sponsor equity     -420,000,000.00
  2031-06-30  exit proceeds     1,134,000,000.00

  Day count             ACT/365F
  Holding period        5.00 years
  Net                   714,000,000.00
  MoIC                  2.70x
  IRR                   21.96%
```

Add `--json` for machine-readable output, and `--convention` to pick the
day-count basis used to annualise.

A deal is described in a file — there are already more inputs than a flag list
carries legibly, and the operating case and debt schedule will add more:

```console
$ capstack deal examples/meridian.json
Project Meridian   (close 2026-06-30)
=====================================

  Entry
    LTM EBITDA                          240.00
    Entry multiple                      11.50x
    Enterprise value                  2,760.00
    Net debt                            345.00
    Equity purchase price             2,415.00

  Sources
    Term Loan B                 1,150.00   issued at 99.50
    Senior secured notes          450.00
    Second lien                   250.00   issued at 98.00
    Rollover equity                85.00   no cash moves
    Cash from balance sheet        45.00
    Sponsor equity                994.14   the plug
                                --------
    Total                       2,974.14

  Uses
    Purchase of equity          2,415.00
    Repay existing debt           410.00
    Transaction fees               38.64   expensed
    Financing fees                 41.25   capitalised
    Original issue discount        10.75
    Cash to balance sheet          40.00
    Change of control payments     18.50   management contracts
                                --------
    Total                       2,974.14

  Entry metrics
    Total leverage                       7.71x
    Equity contribution                 36.8%
    Sponsor ownership                   92.1%
    Total capitalisation              2,929.14
```

It also carries the target's own balance sheet, which is what purchase
accounting is applied to:

```console
$ capstack balance examples/meridian.json
Project Meridian - opening balance sheet   (close 2026-06-30)
=============================================================

  Assets
    Cash                           60.00
    Identifiable assets         1,675.00
    Goodwill                    1,610.00
    Deferred financing costs       41.25
    Unamortised issue discount     10.75
                                --------
    Total assets                3,397.00

  Liabilities
    Debt at face                1,850.00
    Operating liabilities         480.00
    Deferred tax liability         45.00
                                --------
    Total liabilities           2,375.00

  Equity
    Sponsor equity                994.14
    Rollover equity                85.00
    Expensed at close             -57.14
                                --------
    Total equity                1,022.00

    Liabilities and equity      3,397.00

    Net debt                    1,790.00
    Goodwill share of assets       47.4%
```

The same file carries the operating case:

```console
$ capstack project examples/meridian.json
Project Meridian - operating case
=================================

                                       P1        P2        P3        P4        P5
                                  2027-06   2028-06   2029-06   2030-06   2031-06
  -------------------------------------------------------------------------------
  Revenue                        1,605.80  1,722.22  1,825.55  1,912.27  1,979.20
  EBITDA                           261.75    291.49    320.38    347.55    372.09
  Depreciation & amortisation       57.81     62.00     65.72     68.84     71.25
  EBIT                             203.94    229.49    254.66    278.71    300.84

  Cash tax                         -50.98    -57.37    -63.67    -69.68    -75.21
  NOPAT                            152.95    172.11    191.00    209.03    225.63

  Add back D&A                      57.81     62.00     65.72     68.84     71.25
  Capital expenditure              -77.08    -78.36    -78.50    -77.45    -75.21
  Change in working capital        -14.09    -13.04    -11.57     -9.71     -7.50
  Unlevered free cash flow         119.59    142.71    166.65    190.72    214.17

  EBITDA margin                     16.3%     16.9%     17.5%     18.2%     18.8%
  Cash conversion                   45.7%     49.0%     52.0%     54.9%     57.6%
```

And it carries the capital structure, which is where the cash actually goes:

```console
$ capstack schedule examples/meridian.json
Project Meridian - debt schedule
================================

                                      P1         P2         P3         P4         P5
                                 2027-06    2028-06    2029-06    2030-06    2031-06
  ----------------------------------------------------------------------------------
  Unlevered free cash flow        119.59     142.71     166.65     190.72     214.17
  Cash interest                  -141.32    -138.62    -134.23    -128.78    -122.71
  Commitment fees                  -0.73      -0.67      -0.69      -0.74      -0.76
  Levered free cash flow          -22.45       3.42      31.73      61.20      90.71

  Mandatory repayment             -11.50     -11.50     -11.50     -11.50     -11.50
  Cash sweep                        0.00       0.00     -15.17     -37.27     -39.60
  Revolver draw                    13.95       8.08       0.00       0.00       0.00
  Closing cash                     40.00      40.00      45.06      57.48      97.08

  Accrued to balances              11.67      12.25      12.79      13.39      14.01
  Closing debt                  1,864.12   1,872.95   1,859.07   1,823.69   1,786.60

  Closing balances
  Revolving credit facility        13.95      22.03       6.86       0.00       0.00
  Term Loan B                   1,138.50   1,127.00   1,115.50   1,073.59   1,022.48
  Senior secured notes            450.00     450.00     450.00     450.00     450.00
  Second lien                     261.67     273.92     286.71     300.10     314.11

  Leverage                         7.12x      6.43x      5.80x      5.25x      4.80x
  Base rate                        4.25%      3.94%      3.62%      3.31%      3.00%
```

The first two years are the interesting part: levered free cash flow is
negative, the revolver funds the gap, the second lien accrues rather than pays,
and total debt goes *up* before the operating case grows into the structure.

The sweep in this deal steps: three quarters of excess cash flow above 5.50x,
half above 4.50x, a quarter above 3.50x and nothing below. That is why the cash
balance climbs in the last two periods instead of every spare pound going
straight into the term loan.

See [`examples/meridian.json`](examples/meridian.json) for the input. Assumption
series are written the way an operating case is actually described — a bare
number for something flat, `{"ramp": [0.085, 0.035]}` for growth that tapers, or
a list when the years genuinely differ.

Whether the structure is *allowed* to keep running is a separate question:

```console
$ capstack covenants examples/meridian.json
Project Meridian - covenants
============================

                                      P1         P2         P3         P4         P5
                                 2027-06    2028-06    2029-06    2030-06    2031-06
  ----------------------------------------------------------------------------------
  Total net leverage                 n/a      6.29x      5.66x      5.08x      4.54x
    covenant                         n/a      7.50x      6.75x      6.00x      5.50x
    cushion                          n/a      16.2%      16.1%      15.3%      17.4%
    status                             -         ok         ok         ok         ok

  First lien net leverage            n/a      3.80x      3.36x      2.92x      2.49x
    covenant                         n/a      5.25x      4.75x      4.25x      4.00x
    cushion                          n/a      27.5%      29.2%      31.2%      37.8%
    status                             -         ok         ok         ok         ok

  Interest coverage                  n/a      2.09x      2.37x      2.68x      3.01x
    covenant                         n/a      1.73x      1.85x      1.98x      2.10x
    cushion                          n/a      17.6%      22.1%      26.4%      30.3%
    status                             -         ok         ok         ok         ok

  Fixed charge coverage              n/a      1.03x      1.22x      1.42x      1.64x
    covenant                         n/a      1.00x      1.00x      1.00x      1.00x
    cushion                          n/a       1.7%       9.9%      17.1%      23.3%
    status                             -         ok         ok         ok         ok

  Tightest test
    Fixed charge coverage in period 2
    EBITDA projected                  291.49
    Breaches below                    286.53
    Cushion                            1.7%

  No maintenance test is breached across the hold.
```

Each test shows the ratio, the covenant in force that period, and the cushion —
how far EBITDA could fall before the test trips. The cushion is the number worth
reading. All four tests pass comfortably in turns, but the fixed-charge test in
period two survives a fall in EBITDA of only 1.7%, which is a materially
different deal from the one the leverage rows describe.

And finally what the whole thing was for:

```console
$ capstack exit examples/meridian.json
Project Meridian - exit
=======================

  Exit at 2031-06-30
    Exit EBITDA                       372.09
    Enterprise value                4,092.98
    Debt outstanding                1,786.60
    Cash                               97.08
    Cost of sale                       30.70
    Equity value                    2,372.77
    Exit multiple                     11.00x
    Exit leverage                      4.54x

  Equity
                              invested     proceeds         MoIC          IRR
    Sponsor preferred           845.02     1,241.87        1.47x         8.0%
    Sponsor common              149.12       927.34        6.22x        44.1%
    Management rollover          85.00       203.56        2.39x        19.1%
    -------------------------------------------------------------------------
    Total                     1,079.14     2,372.77        2.20x        17.1%
    over 5.00 years

  Where the value came from
    EBITDA growth                   1,519.02
    Multiple change                  -186.04
    Debt paydown                      100.49
    Entry and exit costs             -139.84
                              --------------
    Value created                   1,293.63
```

The equity rows are the point. Bought and sold at a lower multiple than it was
bought at, the deal still returns 2.20x — and the sponsor's preferred earns 8.0%
while the common behind it earns 44.1%, on exactly the same exit. That gap is
the structure doing what the structure is for, and a model that reported one
blended figure for the equity would hide it.

The bridge underneath ties to the change in equity value exactly. It is worth
reading in the order it prints: the business is worth 1,519 more because it
earns more, 186 less because the multiple came in, and 100 more because the
schedule repaid debt out of cash flow — a reminder that in a five-year hold at
this leverage, deleveraging is a rounding error next to growth.

Two assumptions at a time, with the deal rebuilt and re-run per cell:

```console
$ capstack sensitivity examples/meridian.json \
      --rows ebitda-margin:-6,-3,0,3 --columns exit-year:3,4,5,6
Project Meridian - sensitivity
==============================

  IRR
    exit year across, ebitda margin down
    base case 17.1%, marked *

                  3y        4y       5y*        6y
    -6pp     -34.7%!   -16.0%!    -6.8%!    -3.8%!
    -3pp      -3.9%!     3.2%!     6.7%!     7.2%!
    0pp*      16.0%     17.1%     17.1%     15.8%
    +3pp      30.6%     27.2%     24.6%     21.9%

    ! a covenant breaches on this case
    * the assumption the file describes
```

The marks are the part worth reading. Three hundred basis points off the margin
still returns 6.7% over the planned hold, and every cell in that row trips a
maintenance test before it gets there — a return the lenders have the right to
interrupt is not a return. Axes are written the way they are said: levels as
levels (`entry-multiple:11,11.5,12`), shifts in percentage points off the file's
own case (`ebitda-margin:-1.5,0,1.5`). Seven dimensions and seven metrics are
available; `capstack sensitivity --help` lists them.

## Test

```bash
pytest
mypy
```

## Design decisions

**Money is `Decimal`, never `float`.** A debt schedule is a chain of dependent
subtractions running twenty periods deep; binary floating point drifts, and the
drift shows up as a balance that fails to reach zero in the period it should.
Every currency amount in `capstack` is a `Decimal` carried at full precision and
rounded only at the point of presentation.

Rates are a separate matter. Solving for an internal rate of return means root
finding, and root finding wants floats. So the boundary is explicit: cash flows
are exact, the solver works in floating point, and the result is a rate rather
than an amount.

**An ambiguous IRR is reported as ambiguous.** A cash-flow stream that changes
sign more than once can have several rates at which its present value is zero.
A stream of -1,000 / +2,500 / -1,560 returns 20% and it returns 30%; both are
true, and which one a solver reports depends on where it started looking.

`capstack` scans the whole rate range for sign changes, refines every bracket it
finds, and raises `AmbiguousIRR` carrying all the roots rather than picking one.
Refusing to answer is the correct behaviour here — the alternative is a model
that quietly reports the flattering root.

**Sources and uses balance exactly, or the object does not exist.** The check is
in `__post_init__`, and it is equality rather than a tolerance. A funding table
that is out by a few thousand on a three-billion deal has not nearly balanced —
it has a missing line item, and a tolerance would hide precisely the error the
table exists to catch.

The sponsor's cheque is a derived residual rather than an input, which is how it
works on a real deal: leverage is negotiated with lenders, price is negotiated
with the seller, and equity fills whatever gap is left. When that residual comes
out negative the structure is funding itself entirely out of its own borrowing
capacity and paying the sponsor a distribution at close. That is aggressive, not
invalid, so the model labels it rather than refusing it.

**Goodwill is a residual, and the target's own goodwill is not part of it.**
Goodwill on the target's books is the leftover of somebody else's transaction.
It is written off at close and a new residual is struck against the price just
paid; carrying both counts the same intangible twice and flatters the asset side
by the whole of the old number.

A fair-value step-up in a stock deal comes with a deferred tax liability, because
book depreciation rises and tax depreciation does not. Recognising the step-up
without the liability overstates equity by the tax on it. In the shipped example,
180 of step-up at 25% moves only 135 out of goodwill, not 180.

**Working capital reaches cash flow as a movement, not a level.** A business
growing at 8% with working capital steady at 15% of revenue consumes cash every
single period, because the balance is rising and the increase has to be funded —
even though the ratio never moves. Subtracting the balance rather than the change
is the classic error, and it understates cash flow by an order of magnitude.

**A loss carryforward cannot shelter everything.** The pool is capped at a
percentage of taxable income — 80% by default, matching the limitation on US
losses arising from 2018 onward — so a company with large historic losses still
writes a cheque the moment it turns profitable. Modelling the pool without the
cap overstates cash in precisely the years a sponsor is counting on it.

**The interest/balance circularity is solved, not avoided.** Accrue interest on
the average of the opening and closing balance — the right convention when
repayments are spread through the period — and the closing balance depends on
what was repaid, which depends on the cash left after interest, which depends on
the balance the interest was accrued on. Once repayments are capped at balances
and sweeps at available cash there is no closed form, so the engine iterates to
a fixed point and reports the residual it reached. The alternative, accruing on
the opening balance, overstates interest in every period a structure
deleverages — which is every period a buyout is working.

The map contracts by roughly `rate x year_fraction / 2` per turn, so a full step
settles in about ten iterations; the step is halved if it ever fails to reduce
the residual. Where no fixed point exists — a PIK rate at or past the pole at
`pik_rate x year_fraction = 2` — the engine raises rather than returning its last
iterate, because an unconverged schedule is not an approximate answer, it is a
set of balances that do not reconcile with the interest charged against them.

**A cash sweep is measured on the period's excess cash flow.** Not on the cash
balance. Cash that a partial sweep deliberately left behind is not excess cash
flow any more, and a credit agreement does not reach it again at the next test
date. Sweeping the balance instead takes back half the retained cash next
period, then half of that, until a negotiated 50% sweep has quietly become a
100% one — which makes the sweep percentage, the most argued-over number in the
credit agreement, do nothing at all.

**Headroom is measured in EBITDA, not in turns.** The distance from 5.20x to a
6.00x covenant is 0.80x. That is arithmetic, and it is not comparable across
tests: 0.80x of leverage headroom and 0.80x of interest-cover headroom describe
completely different amounts of trouble. Every observation therefore carries the
EBITDA at which the test would trip and the shortfall as a percentage of the
EBITDA projected. In the worked example the leverage tests look loose and the
fixed-charge test survives a 1.7% miss, which is the fact worth knowing and the
one the turns hide.

The debt and the charges are held at their projected levels while EBITDA is
flexed, which is why this is called a cushion and not a forecast. A business
actually earning less would sweep less and carry more debt into the following
year, so the true cushion is a little thinner than the reported one, never
thicker.

**An undefined ratio is not a pass and is not a blank.** A period with no
earnings has no leverage ratio. Whether that is a breach depends on what is
behind it: a business with no debt cannot be over-levered, and a business with
debt and no earnings is exactly the case the covenant exists to catch. Both
report no ratio; one passes and one breaches, and the report says which and why
rather than leaving a reader to infer it from an empty cell.

**The sweep grid is certified in arrears.** Real agreements sweep 50% of excess
cash flow, stepping down as leverage falls. Reading the step off the leverage of
the period being swept would make the rate depend on the closing balance, which
depends on the sweep — a second circularity, and a step function dropped into
the middle of the interest fixed point, where it can oscillate between two rungs
indefinitely. The step is resolved against the leverage at the most recent test
date instead. That is not a modelling convenience: an excess cash flow payment
is made in arrears, at a rate set by a certificate signed before the cash was
counted.

**A structure carrying both a grid and a flat sweep rate is refused.** It has
two answers to the same question, and silently preferring one of them hides a
contradiction in the description of the deal rather than resolving it.

**Growth is valued at the entry multiple and the multiple at exit EBITDA.** The
cross term between the two — the extra turns earned on the extra EBITDA — has to
be assigned somewhere, and the choice changes the story. Putting it in the
multiple line is the conservative reading: the alternative flatters the growth
line in precisely those deals where the multiple expanded, which are the deals
where a sponsor least wants to be asked how much of the return was theirs.

**The attribution is computed, not plugged.** The costs line could be defined as
whatever the other three do not explain, and the bridge would then tie by
construction and mean nothing. It is instead computed from the deal — the fees
and issue discount the equity funded at close, plus the cost of selling — so the
four components summing to the change in equity value is a check that can fail.
It is asserted in the test suite at three different exit multiples.

**The bridge is drawn before limited liability, the distribution after it.**
Equity in a leveraged company is an option: shareholders in a business worth
less than its debt hand the keys to the lenders rather than receiving a bill.
So the equity value is floored at zero when it is distributed. It is *not*
floored in the attribution, because the floor is a legal fact rather than a
source of value, and folding it into the costs line would misattribute a loss
the lenders absorbed to fees the sponsor paid. The two are reported side by
side, with the difference named.

**A security that was wiped out has no rate of return.** There is no rate at
which nothing back is a return, and the solver's search range does not reach
-100% because the discount factor is singular there. Rather than report the
floor of the range as though it were an answer, the row carries no rate and the
reason travels with it.

**Falling below the minimum cash balance is not the same as running out.** A
business that ends a period on less cash than its own policy requires has a
covenant conversation ahead of it. One that ends below zero has an unpaid bill.
The model reports them separately, and plugs only the second — notionally, and
by name — so the periods after it stay readable instead of compounding a deficit
that has already been reported.

**Debt is carried at face, not at proceeds.** A tranche placed at 99.5 raises
99.5 and owes 100. The half-point is a use of funds. Carrying the tranche at
proceeds understates leverage and understates every interest payment that
follows it, because interest accrues on what is owed rather than on what was
received.

## Licence

MIT
