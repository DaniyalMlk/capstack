# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: early. The numerics layer and the transaction are in — exact money, day
counts, period grids, the return measures, and a sources and uses table that
balances. See [ROADMAP.md](ROADMAP.md) for what is next.

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

See [`examples/meridian.json`](examples/meridian.json) for the input.

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

**Debt is carried at face, not at proceeds.** A tranche placed at 99.5 raises
99.5 and owes 100. The half-point is a use of funds. Carrying the tranche at
proceeds understates leverage and understates every interest payment that
follows it, because interest accrues on what is owed rather than on what was
received.

## Licence

MIT
