# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: early. Three layers are in — the numerics (exact money, day counts,
period grids, the return measures), the transaction (entry valuation and a
sources and uses table that balances), and the operating case (drivers through
to unlevered free cash flow). The debt schedule is next; see
[ROADMAP.md](ROADMAP.md).

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

See [`examples/meridian.json`](examples/meridian.json) for the input. Assumption
series are written the way an operating case is actually described — a bare
number for something flat, `{"ramp": [0.085, 0.035]}` for growth that tapers, or
a list when the years genuinely differ.

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

**Debt is carried at face, not at proceeds.** A tranche placed at 99.5 raises
99.5 and owes 100. The half-point is a use of funds. Carrying the tranche at
proceeds understates leverage and understates every interest payment that
follows it, because interest accrues on what is owed rather than on what was
received.

## Licence

MIT
