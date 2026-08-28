# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: the engine is complete through phase seven, and phase eight — the
things that happen between buying a business and selling it — is under way. What
is there: the numerics (exact money, day counts, period grids,
the return measures), the transaction (entry valuation, a sources and uses table
that balances, and the opening balance sheet after the recapitalisation), the
operating case (drivers through to unlevered free cash flow), the capital
structure (interest, amortisation, a cash sweep by seniority and a revolver that
runs both ways), the covenants (maintenance tests, headroom measured in EBITDA,
and a sweep that steps with a leverage grid), and the exit (equity value,
returns by security through a preferred waterfall, and a value-creation bridge),
and the analysis on top of them (two-dimensional sensitivity with the whole
engine rebuilt and re-run at every cell, break-evens solved along any
assumption, and the committee memo that assembles all of it), together with the
management incentive plan that sits between the preferred and the common — an
option pool with a strike, a vesting schedule, and a ratchet that steps with the
sponsor's own return — the dividend recapitalisation, which raises debt part-way
through a hold and pays it straight out to the shareholders, the add-on
acquisition, which buys earnings during the hold and blends them into the entry
multiple, and the refinancing, which retires a facility early and reports
whether the lower coupon covered the premium. See [ROADMAP.md](ROADMAP.md).

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

Management get paid too, and the plan that pays them comes out of the common:

```console
$ capstack exit examples/kestrel.json
Project Kestrel - exit
======================
  ...
  Equity
                              invested     proceeds         MoIC          IRR
    Sponsor equity              407.62       936.50        2.30x        18.1%
    Management rollover          24.00       139.94        5.83x        42.3%
    -------------------------------------------------------------------------
    Total                       431.62     1,076.43        2.49x        20.0%
    over 5.00 years

  Management incentive plan
    Vested                           100.0%
    Share of the pot                   5.7%
    Residual before the plan        1,093.28
    Strike paid in                     47.96
    Pot divided                     1,141.24
    Entitlement                        64.81
    Paid to management                 16.85
    What management are paid is what the common give up, to the penny.
```

Kestrel's pool is 10% of the fully diluted equity, struck at what the equity was
worth on the day it closed, on a ratchet that pays 5% of the pot up to a 2.0x
sponsor return and 10% above it. The sponsor lands on 2.30x, so the second band
is partly in play and the blended take is 5.7% — a number that is not any of the
inputs. Every return above it is quoted net of the plan, which is the only
version worth quoting: a sponsor multiple struck before management are paid is a
multiple on money somebody else receives.

Not every deal is bought once and sold once. Kestrel raises 80 of incremental
debt at the end of the third year and pays the proceeds out:

```console
$ capstack report examples/kestrel.json
  ...
  Paid during the hold
  --------------------

  98.2 reached the equity before the exit, funded by 80.0 of new debt. The
  money multiple moves -0.02x and the rate of return moves +0.8pp: the same
  money, banked earlier, less what the debt cost to carry.

    Distributed during the hold   98.2
    Incremental face raised       80.0
    Cost of raising it             1.8   fees and issue discount
    Money multiple, as run       2.47x
    Money multiple, held flat    2.49x
    Rate of return, as run       20.9%
    Rate of return, held flat    20.0%

    Date        Payment                 Amount  Years  Preferred  Common
    ----------  ----------------------  ------  -----  ---------  ------
    2029-09-30  Distribution, period 3    98.2   3.00        0.0    98.2

    Dividend recapitalisation put 0.84x of leverage back on, taking net debt
    from 3.13x to 3.97x of EBITDA.
```

The four figures in the middle are the point. Nothing about the business
changed — same earnings, same multiple, same buyer — and the two measures
disagree about whether anything happened. The multiple falls slightly, because
the new debt costs interest for two years and the fees were real. The rate rises,
because it is the only measure that knows the sponsor had the money in 2029
rather than 2031. The "held flat" rows are the same deal run a second time with
the event stripped out, which is the only way to say what the event was worth
rather than merely that it occurred.

A deal that buys other businesses while it holds this one is described under
`acquisitions`, and each purchase gets its own funding table:

```console
$ capstack acquisitions examples/thornbury.json
Project Thornbury - acquisitions
================================

  Halloway  (end of period 1)
  ---------------------------
    EBITDA acquired                       6.50
    Multiple paid                        6.75x
    Enterprise value                     43.88
    Synergies, over 2 periods             0.90
    Multiple after synergies             5.93x
    Transaction fees                      0.66
    Integration cost                      1.20
    Total uses                           45.73
    Face drawn                           44.00
    Debt proceeds                        42.90
    Funded from cash                      2.83
    Capital deployed                     46.83
    Cash after                           16.28
    Leverage after                       5.06x
    Turns added                         +0.92x
  ...
  Blended entry
    Platform enterprise value           504.00
    Platform EBITDA                      48.00
    Platform multiple                   10.50x
    Acquired enterprise value           134.25
    EBITDA acquired                      20.00
    Combined EBITDA                      68.00
    Capital deployed                    647.09

    Blended multiple                     9.39x
    After synergies                      9.01x
    On capital deployed                  9.52x
    Multiple arbitrage                  +1.11x
    Bought, not built                    29.4%
```

The last five lines are what a buy-and-build is argued on. A platform bought at
10.50x that adds three businesses between 6.25x and 7.00x has an entry multiple
of 9.39x, and it is that number the exit multiple has to be compared against —
quoting the platform's 10.50x flatters the deal by exactly the arbitrage. The
three readings above it answer different questions. After synergies is 9.01x,
which credits earnings that have not been earned yet. On capital deployed is
9.52x, which counts the fees, the discount and the integration cost that a
multiple quoted on enterprise value leaves out: doing five transactions instead
of one is not free, and the gap between 9.39x and 9.52x is what it cost.

The purchases then run through everything downstream without being told about.
The acquired earnings join the operating case from the following period at their
own margin, growing on their own base; the debt raised for them is swept,
amortised and tested by the covenants; and the memo reports the programme
against the same deal with the purchases stripped out:

```console
$ capstack report examples/thornbury.json
  ...
  Bought during the hold
  ----------------------

  3 acquisitions added 20.0 of run-rate EBITDA at 9.39x blended against a
  platform bought at 10.50x, 1.11x of arbitrage. Against the platform run on
  its own the money multiple moves +0.43x and the rate of return moves
  +4.5pp.

    Business         Closes  EBITDA  Multiple  Price  New debt  From cash
    ---------------  ------  ------  --------  -----  --------  ---------
    Halloway         P1         6.5     6.75x   43.9      44.0        2.8
    Ferrand Group    P2         8.0     7.00x   56.0      56.0        3.7
    Calder Services  P3         5.5     6.25x   34.4      33.0        3.5

    25.2 of the 94.1 of EBITDA the exit is priced on was bought rather than
    built, which is 26.7% of it. An exit multiple argued from the platform's
    own growth has to carry that share too.
```

A facility taken out early is described under `refinancings`, and the memo
reports the trade rather than the new coupon:

```console
$ capstack report examples/thornbury.json
  ...
  Refinanced during the hold
  --------------------------

  200.9 of paper was retired early at a cash cost of 4.9. The lower coupon
  does not earn back that over the hold that remains: 2.5 of interest saved.
  A further 2.4 of capitalised fees was written off, which is a charge
  against earnings and not against cash.

    Face retired early                 200.9
    Face of the new paper              195.0
    Call premiums paid                   2.0
    Cost of the exercise                 4.9   premium, fees and discount
    Interest saved over the remainder    2.5   undiscounted, before amortisation
    Fees written off                     2.4   non-cash; no balance here moves

    Term Loan B repricing took 200.9 out at 7.7% and replaced it at 6.4%,
    saving 2.5 a period with 1 of them left. Over that remainder the saving
    does not cover what it cost: 2.5 against 4.9.
```

That verdict is the reason the section exists. A repricing is always attractive
stated as a spread — 130 basis points off the coupon — and this one still does
not clear, because the premium and the fees are paid at once while the saving
arrives a period at a time and there is only one period left. The example ships
with a decision the model argues against, which is more useful than one where
everything works.

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

And the whole thing as one document:

```console
$ capstack report examples/meridian.json
Project Meridian — investment committee (close 2026-06-30)
==========================================================

  The transaction
  ---------------

  240.0 of LTM EBITDA at 11.50x, funded at 7.71x of gross debt and 7.46x net
  of the cash left on the balance sheet.
  ...

  Where the case stops working
  ----------------------------

  The sponsor gets its capital back at 7.50x, which is 4.00x below the
  11.50x paid going in.

    Question                                       Answer
    --------------------------------------------  -------
    Exit multiple returning capital and no more     7.50x
    Exit multiple returning twice capital          10.42x
    Margin shift tripping the first covenant      -0.35pp
    Opening leverage tripping the first covenant    7.94x
```

`--markdown` for anything that has to be pasted somewhere, `--json` for
anything that would rather lay the memo out itself, and `--no-breakevens` to
skip the last section, which is the expensive part.

That last section is the reason the report exists. Everything above it restates
the base case, which is the one thing the reader already believes. The
break-evens say where it stops: this structure has 4.00x of room on the exit
multiple and 35 basis points of room on margin before a lender has a right to
accelerate, and those two facts are what the meeting is actually about.

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

**A preferred paid down early accrues on what is left.** Once a deal can pay
its shareholders more than once, a preferred claim stops being "the cheque plus
a hold's worth of coupon". A distribution meets the accrued return first and
capital second — which is the ordinary waterfall ordering, and the thing that
makes "how much capital is still outstanding" answerable at all after a partial
repayment. The remaining capital is what the coupon runs on from there.

Charging the coupon on the original cheque after part of it has been repaid
overstates the preferred's claim at exit and understates everything ranking
behind it, which on a structure with preferred ahead of common is a transfer
between two holders who both read the model.

**A recapitalisation is reported against the deal without it.** Interest and
fees mean the money multiple falls slightly and the rate of return rises
materially: the same money, banked earlier, less what the debt cost to carry.
Reported on its own, that says the sponsor received some money early. Reported
against the same deal run a second time with the event stripped out, it says
what the event was worth. The second run costs a full pass of the engine and is
worth it, because the counterfactual is the entire content of the claim.

**An acquired business is carried as its own stream, not as a lift to the
platform's revenue line.** The natural implementation adds the bought revenue
into the base that compounds, and it is wrong twice over: the acquired base then
grows a second time next period, and the earnings inherit the platform's margin
whatever they were actually bought on. Carrying the purchase separately keeps
both honest — it compounds from its own run-rate at its own margin — and it also
buys the one figure a buy-and-build cannot be judged without, which is how much
of the exit earnings were bought rather than built.

**Cash is the plug for an acquisition, and a purchase the balance sheet cannot
fund is refused.** The draws raise what they raise; whatever is left of the
price comes off the cash the business is holding. That means the funding table
cannot fail to balance, and reduces the failure modes to one real question:
whether the business could actually pay. When it cannot, the answer is an error
naming what was needed and what was there, not a quietly smaller purchase. A
model that pays less than the file describes has answered a question nobody
asked.

**An acquisition in the final period is refused rather than modelled.** It would
pay cash for earnings no period ever records, and the exit — priced on the final
period's EBITDA — would value nothing at all. The deal would then report a
purchase price out, no earnings in, and a bridge blaming the shortfall on the
operating case. Bringing the purchase forward or lengthening the projection is
what such a file actually means, and saying so is more useful than modelling the
nonsense faithfully.

**Synergies are quoted twice and neither is the headline.** The blended multiple
holds them out, because a multiple struck on earnings that do not exist yet is a
forecast wearing the clothes of a fact. The synergised multiple credits them,
because it is the number the price was defended with and hiding it does not make
the argument go away. Phasing is explicit for the same reason: an add-on that
pays for itself in year one and one that pays for itself in year three are
different deals, and underwriting the first when the second is what happens is
how a roll-up gets into trouble.

**A refinancing is judged on the hold that remains, not on the spread.** The
premium and the fees on the new paper are paid at once; the lower coupon arrives
a period at a time. Two years from an exit there is frequently not enough left
of the hold to earn back what the exercise cost, and a model reporting the new
coupon without that comparison has made the case for something it never tested.
The saving is deliberately struck on the balance retired and before amortisation
and the sweep reduce it, which makes it an upper bound — the direction a cost
comparison should err in.

**Unamortised financing fees written off are reported, and reported apart from
anything that is cash.** The fees on the original paper were capitalised at
close and written down over its life; retiring it early charges whatever is
left in one go. Nothing moves in the bank account, no balance in this engine
changes and the return does not shift by a basis point — which is exactly why
it has to sit outside the cash cost rather than inside it. It is reported at all
because it is real in a real set of accounts, and a reader who meets the number
for the first time somewhere else has been failed by the model.

**A takeout repays before it draws.** The order only shows when the new paper is
the same facility at a new price, and then it decides the answer: repaying first
makes a repricing net to the difference, while drawing first carries both
balances for an instant and reports a figure the credit agreement never showed.
The balance retired is the one left after the period's own amortisation and
sweep have run, because that is the balance the notice would be served on.

**A ratchet is written as marginal bands, because the obvious reading is
circular.** A management pool that steps up as the sponsor clears hurdles is the
common structure and the awkward one to model: the pool's share depends on the
sponsor's return, and the sponsor's return depends on the pool's share. Models
that write down the circle either iterate to whatever they converge on, or test
the hurdle on a pre-dilution figure and leave the reader to discover that 2.0x
meant 2.1x before management were paid.

`capstack` writes the ratchet the way a well-drafted one reads: a *marginal*
share of the proceeds in each band above a hurdle, rather than a higher share of
everything once the hurdle is cleared. That makes the sponsor's post-ratchet
proceeds continuous and strictly increasing in the sale price, so each band's
boundary can be solved in closed form from the band below it — no iteration, and
the hurdles mean what they say. There is a test that re-derives the first
boundary from a finished deal and asserts the sponsor lands on exactly 2.0x
there.

The retroactive alternative is a real structure and it is deliberately not
modelled. It is discontinuous: a penny more of enterprise value can leave the
sponsor with less money than it had a penny earlier, and a solver asked where
the hurdle binds has to choose between two answers on either side of a cliff.

**An option pool is settled by the treasury method, and struck at cost means
struck at cost.** The exercise proceeds join the pot before the pot is divided,
so a pool holding a tenth of the fully diluted equity does not hold a tenth of
the residual — it holds a tenth of the residual plus its own strike, less the
strike. Below the point where those two are equal the options lapse: management
are paid nothing and the common are not diluted at all.

The strike itself is described as a multiple of the equity value at close rather
than as an amount, so it moves with the entry multiple across a sensitivity grid
instead of pricing every column against the base case. The gross-up in that
derivation is easy to get wrong and worth stating: the existing holders' cheque
buys them the share of the company the pool does *not* hold, so the fully diluted
value at close is that cheque divided by one less the pool's share. Struck there,
a plan is exactly at the money on a deal that creates no value. The naive
derivation — the pool's share times the cheque — pays management something on a
deal that created nothing, which is not what anyone signing it believed.

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

**Break-evens are bisected, not extrapolated.** Each evaluation rebuilds and
re-runs the whole engine, so the function being solved is expensive — but more
to the point it is not smooth. A cash sweep that steps at a leverage rung and a
covenant that starts testing in period two put real discontinuities in these
curves, and a secant or Brent step assuming local linearity will extrapolate
across one and return a crossing that is not there. Halving an interval assumes
nothing but a sign change. The bracket is required rather than guessed, because
an open-ended search over an entry multiple eventually wanders into prices at
which the deal does not describe a transaction at all.

**A sensitivity cell is the whole engine, not a gradient.** Raising the entry
multiple raises the price, which the funding table absorbs into the sponsor
cheque, which changes the capital every multiple in the column is measured
against — and, since the debt did not move, changes opening leverage and so the
sweep step and so the debt outstanding at exit. Lowering the *exit* multiple
touches the last period and nothing upstream. A linearisation reports the two as
mirror images; they are not, and the deals that get done live in that gap.

**Equity is described rather than measured.** A security funded as a share of
the sponsor cheque keeps the description, not the amount. The sponsor's
contribution is the plug that balances the funding table, so it moves whenever
anything else does, and a stack that remembered the amount would report every
multiple in a sensitivity column against the base case's denominator.

**Debt is carried at face, not at proceeds.** A tranche placed at 99.5 raises
99.5 and owes 100. The half-point is a use of funds. Carrying the tranche at
proceeds understates leverage and understates every interest payment that
follows it, because interest accrues on what is owed rather than on what was
received.

**Amortisation is struck against a basis, and the basis is a ledger.** A credit
agreement writes the instalment as a fraction of face — 1% a year on a term loan
means 1% of what was borrowed, not 1% of what is left, which is why a sweep
running ahead of the schedule does not reduce what is contractually due next
period. Reading "what was borrowed" as the face drawn at close is right for
paper placed at close and wrong in both directions for anything else: a
delayed-draw facility repays nothing however much is taken down on it later, an
incremental facility grows the balance without growing the instalment, and a
facility retired at a refinancing keeps amortising against face that no longer
exists. So the basis is carried across the hold rather than read off the
tranche. It opens at the face drawn at close, rises by incremental face taken
down at a period boundary, and goes to zero when the facility is retired —
which means face drawn at the end of one period first amortises in the next,
because the instalment for a period is struck on the basis that period opened
on. The schedule prints the basis wherever it moves, so a step in the instalment
has a visible cause.

## Licence

MIT
