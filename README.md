# capstack

A leveraged buyout engine.

`capstack` builds the model a deal team argues over: what the business is bought
for, how the purchase is funded, what the operating case throws off, how the debt
gets paid down, whether the covenants hold, and what the sponsor makes on the way
out.

Status: early. See [ROADMAP.md](ROADMAP.md) for what is built and what is next.

## Install

```bash
python -m pip install -e ".[dev]"
```

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

## Licence

MIT
