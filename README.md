# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: early. The numerics layer is in — exact money, day counts, period grids
and the return measures. See [ROADMAP.md](ROADMAP.md) for what is next.

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

## Licence

MIT
