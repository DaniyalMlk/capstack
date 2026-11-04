"""Every documented option, and whether anything outside its module chooses it.

An optional parameter with a default is a claim: that the caller may choose, and
that choosing works. A parameter no call site anywhere ever supplies has never
had that claim tested. The code that reads it runs, so coverage is satisfied,
but it only ever runs with the one value the author happened to pick. Two of the
defects found in this family of repositories were exactly that shape — a
documented parameter that only ever got its default, and was wrong for every
other value — so the question is asked mechanically here rather than
remembered.

The gate walks the package with :mod:`ast` and collects two kinds of option:

* every parameter with a default on every public function or method, and
* every field with a default on a type a caller constructs.

The second clause is the one that needs stating. A frozen dataclass this library
*returns* also has defaulted fields, and those are not offers — nobody chooses
``CoverageTest.advisory``, the test that produced it fills it in — so a result
type's defaults are not options and are not asked about. Which types are results
is read off the source rather than judged: a type named in the return annotation
of any public function or method in the package is a result. That rule is worth
more than it looks. A first draft asked instead whether anything outside the
declaring module constructs the type, and under it ``GeneralisedPareto`` and
``LossTail`` became bags of options because the tests build one by hand to feed
the next function along — which is a test needing an instance, not a caller
choosing a standard error.

**Supply is counted positionally as well as by keyword.** In Python
``realised_variance(bars, Estimator.PARKINSON)`` chooses an option exactly as
``estimator=`` does, and a first draft of this probe looked only at keywords. It
reported the whole six-way estimator dispatch as unexercised while the tests
were picking all six of them positionally. A gate that cries wolf gets an
exemption list bolted onto it within a week, and then it is decoration, so the
probe resolves a call by name and counts its positional arguments against the
signature.

There is no exemption mechanism, and adding one is the first thing that will be
wanted the next time this fails. It is the wrong thing: an option nobody can be
bothered to choose once in a test is an option to delete.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "capstack"
SEARCH = ("src", "tests", "examples", "docs")


@dataclass(frozen=True, slots=True)
class Option:
    """One defaulted parameter or field, and where it is declared."""

    owner: str
    name: str
    module: Path

    def __str__(self) -> str:
        return f"{self.owner}({self.name}=)"


@dataclass(frozen=True, slots=True)
class Signature:
    """A callable's positional parameter names, for resolving positional calls."""

    names: tuple[str, ...]


def _sources(*directories: str) -> list[Path]:
    found: list[Path] = []
    for directory in directories:
        base = ROOT / directory
        if base.exists():
            found.extend(sorted(base.rglob("*.py")))
    return found


def _returned(annotation: ast.expr | None, into: set[str]) -> None:
    """Every bare name in a return annotation, however it is wrapped."""
    if annotation is None:
        return
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            into.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A quoted forward reference, which is how a classmethod names its
            # own class before the class exists.
            into.add(node.value.strip().strip("\"'"))
        elif isinstance(node, ast.Attribute):
            into.add(node.attr)


def _survey() -> tuple[list[Option], dict[str, Signature], set[str]]:
    """Declared options, callable signatures, and the types the package returns."""
    options: list[Option] = []
    signatures: dict[str, Signature] = {}
    results: set[str] = set()
    annotations: dict[str, set[str]] = {}
    fields: dict[str, list[tuple[str, bool]]] = {}
    declared_in: dict[str, Path] = {}

    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf8"), str(path))

        def walk(node: ast.AST, prefix: str = "", module: Path = path) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    if child.name.startswith("_"):
                        continue
                    _returned(child.returns, results)
                    arguments = child.args
                    names = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
                    if names and names[0] in ("self", "cls"):
                        names = names[1:]
                    signatures[child.name] = Signature(tuple(names))
                    required = len(names) - len(arguments.defaults)
                    for name in names[required:]:
                        if not name.startswith("_"):
                            options.append(Option(prefix + child.name, name, module))
                    for argument, default in zip(
                        arguments.kwonlyargs, arguments.kw_defaults, strict=True
                    ):
                        if default is not None and not argument.arg.startswith("_"):
                            options.append(Option(prefix + child.name, argument.arg, module))
                elif isinstance(child, ast.ClassDef):
                    if child.name.startswith("_"):
                        continue
                    declared_in[child.name] = module
                    own: list[tuple[str, bool]] = []
                    mentioned: set[str] = set()
                    for statement in child.body:
                        if isinstance(statement, ast.AnnAssign) and isinstance(
                            statement.target, ast.Name
                        ):
                            own.append((statement.target.id, statement.value is not None))
                            _returned(statement.annotation, mentioned)
                    annotations[child.name] = mentioned
                    fields[child.name] = own
                    signatures.setdefault(
                        child.name, Signature(tuple(name for name, _ in own))
                    )
                    walk(child, child.name + ".")

        walk(tree)

    # A type handed back inside something handed back is handed back too: a
    # result's own fields can name types that are never a return annotation of
    # their own, and without this closure their defaults would be read as
    # offers.
    frontier = set(results)
    while frontier:
        reached: set[str] = set()
        for name in frontier:
            for mentioned_type in annotations.get(name, set()):
                if mentioned_type not in results:
                    reached.add(mentioned_type)
        results |= reached
        frontier = reached

    for name, own in fields.items():
        if name in results:
            continue  # A type the package returns: its defaults are slots, not offers.
        for field, has_default in own:
            if has_default and not field.startswith("_"):
                options.append(Option(name, field, declared_in[name]))

    return options, signatures, results


def _supplied(signatures: dict[str, Signature]) -> dict[str, set[Path]]:
    """Every parameter name any call site supplies, and the files that do it."""
    places: dict[str, set[Path]] = defaultdict(set)
    for path in _sources(*SEARCH):
        tree = ast.parse(path.read_text(encoding="utf8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg is not None:
                    places[keyword.arg].add(path)
            called = node.func
            name = (
                called.id
                if isinstance(called, ast.Name)
                else called.attr
                if isinstance(called, ast.Attribute)
                else None
            )
            signature = signatures.get(name) if name is not None else None
            if signature is None:
                continue
            for index, _ in enumerate(node.args):
                if index < len(signature.names):
                    places[signature.names[index]].add(path)
    return places


OPTIONS, SIGNATURES, RESULTS = _survey()
SUPPLIED = _supplied(SIGNATURES)


def _unexercised() -> list[str]:
    found: set[str] = set()
    for option in OPTIONS:
        places = SUPPLIED.get(option.name, set())
        # An assignment inside the declaring module is usually the default being
        # written down rather than a caller choosing, so it does not count alone.
        if not places or places == {option.module}:
            found.add(str(option))
    return sorted(found)


class TestTheSurveyItself:
    def test_it_surveyed_the_source_it_claims_to_have_surveyed(self) -> None:
        # "Nothing is unexercised" passes trivially over an empty survey, so the
        # survey is asserted before the claim is made.
        assert len(_sources(*SEARCH)) > 60
        assert len(OPTIONS) > 150
        assert len(SIGNATURES) > 400
        named = {str(option) for option in OPTIONS}
        for certainly_an_option in (
            "brent_root(xtol=)",
            "npv_periodic(first_period_offset=)",
            "Grid.run(levered=)",
            "levered_schedule(opening_cash=)",
            "Rate(note=)",
        ):
            assert certainly_an_option in named

    def test_a_result_type_is_not_treated_as_a_bag_of_options(self) -> None:
        # Nobody chooses a debt period's call premium or a grid's cells: the
        # schedule that produced them filled them in. Each of these is named in
        # the return annotation of some public callable, which is the whole of
        # the rule -- not an exemption list. A type that stops being returned
        # joins the gate on the next run without anyone editing here.
        for returned in ("DebtPeriod", "DebtSchedule", "Grid", "Outcome"):
            assert returned in RESULTS
        named = {str(option) for option in OPTIONS}
        assert not [option for option in named if option.startswith("DebtPeriod(")]

    def test_a_positional_argument_counts_as_choosing(self) -> None:
        # brent_root's function and bracket are supplied positionally
        # everywhere, so a keyword-only probe would report them unexercised --
        # except that they have no defaults. The clause still matters for every
        # callable whose defaulted parameters can be reached positionally, and
        # the resolution is asserted here.
        assert SIGNATURES["brent_root"].names == ("f", "lower", "upper")
        assert "f" in SUPPLIED
        # The three tolerances are keyword-only and chosen by name.
        for tolerance in ("xtol", "rtol", "max_iter"):
            assert tolerance in SUPPLIED

    def test_the_owner_of_a_supplied_name_is_not_checked(self) -> None:
        """A known weakness, written down rather than discovered again.

        Supply is counted by parameter *name*, anywhere in the tree, so a
        parameter forwarded down a call chain -- ``tolerance=tolerance`` --
        registers as a caller having chosen it, and so does the same name
        supplied to an unrelated callable. Both happen here. ``tolerance``,
        ``max_iterations``, ``opening_cash`` and ``periods_per_year`` appear
        almost only as forwards inside the engine; ``months`` is supplied at
        calls to the period helpers and never at the four callables that
        declare it as an option. Keying supply on the owning callable as well
        as the name raises the count of unexercised options here from one to
        twenty-four.

        That stricter probe belongs in its own change, with each of the
        twenty-four either chosen or deleted, so this gate matches its siblings
        for now and the weakness is recorded where the next reader will see it
        rather than found again.
        """
        # Both names satisfy the probe, which is the weakness rather than a
        # result: the options they belong to have still only ever run at their
        # defaults.
        assert "tolerance" in SUPPLIED
        assert "months" in SUPPLIED
        named = {str(option) for option in OPTIONS}
        assert "OperatingModel.trailing_ebitda(months=)" in named
        assert "levered_schedule(tolerance=)" in named


class TestEveryOptionIsChosenSomewhere:
    def test_none_is_left_only_ever_at_its_default(self) -> None:
        assert _unexercised() == []
