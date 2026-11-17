"""The package's public surface is what its modules declare.

``src/capstack/__init__.py`` re-exports the package's public names, and it is a
hand-maintained list. A hand-maintained list is a list with something missing
from it, and what was missing was reachable from what was present:

    >>> import capstack
    >>> deal = capstack.load_deal("examples/kestrel.json")
    >>> deal.fee_schedule(method=capstack.FeeMethod.STRAIGHT_LINE)
    AttributeError: module 'capstack' has no attribute 'FeeMethod'

``Deal.fee_schedule`` takes a ``FeeMethod`` and returns a ``FeeSchedule``, and
``fees`` had no import line in ``__init__.py`` at all: a whole module, five
declared names. ``Deal.levered`` returned a ``LeveredSolution`` and raised a
``TaxCircularityError``, neither at the root, so its failure could not be caught
by name. Thirty-four names across eleven modules were declared public by their
own module and absent from the package.

Nothing noticed because every test imports from a submodule — ``from
capstack.fees import ...`` — which is the right style for a test and exactly
the style that cannot see a missing re-export. The README is written in CLI
invocations, so it never imported anything either.

So the surface is recomputed here from the modules rather than compared against
a second list that would need the same remembering. **A module's own
``__all__`` is the authority**: what is asserted is a disagreement *inside* the
package, a module declaring a name public while the root does not carry it.

The check that names the symptom rather than the cause: a type that appears in
the signature of anything reachable from the root is itself reachable from the
root. Seven signatures failed it.

**Equality, once the modules could carry it.** Containment let a module define a
public name and leave it out of its ``__all__``, which exempted that name from
every check here. Thirty-seven were in that position; three had never been used
and were removed, and the rest carried ``#:`` doc-comments — documented
attributes, most of them the default of a public parameter or a bound a public
docstring names — and were declared. So a module's ``__all__`` is required to
hold every public name its source defines, and the root is required to be
exactly the union of those declarations.

That probe reads source with :mod:`ast`, not the imported module. An attribute
sweep has to tell a name the module defines from one it imports, and the usual
test — ``obj.__module__`` — does not exist on a bare ``str``, ``int``,
``float`` or ``tuple``, and on a ``Decimal`` it names ``decimal``. Thirty-one of
the thirty-four names declared here are one of those.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import typing
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

import capstack
from capstack.fees import FeeMethod, FeeSchedule
from capstack.spec import Deal

#: Not part of the importable surface: ``cli`` is the console entry point.
NOT_SURFACE = frozenset({"cli", "__main__"})

#: Modules that declare no ``__all__``. Pinned rather than discovered, so that a
#: new module arriving without one is a decision somebody makes here instead of
#: a silent exemption from every check in this file.
NO_DUNDER_ALL: frozenset[str] = frozenset()

#: Names in the root's ``__all__`` that no module declares. ``__version__`` is
#: assigned in ``__init__.py`` itself, so it is the one legitimate entry.
ROOT_ONLY = frozenset({"__version__"})

#: Source written to exercise :func:`public_names_in`, not to be a module.
PROBE_SOURCE = '''
"""A docstring, which defines nothing."""

from __future__ import annotations

import math                      # an import is not a definition
from decimal import Decimal

LABEL = "Sponsor equity"         # a bare str: no __module__ to sweep by
BRACKET = (1, 30)                # a bare tuple: likewise
ANNOTATED: int = 2               # an annotated assignment
PAIR_ONE = PAIR_TWO = 3          # one statement, two names
_PRIVATE = 4                     # underscore: not public
__all__ = ["LABEL"]              # the declaration is never a definition


def public_function() -> None:
    """A function."""
    local_name = 5               # a name in a body is not top level


async def public_coroutine() -> None:
    """An async function, which this package has none of."""


class PublicClass:
    """A class."""

    attribute = 6                # a class attribute is not a module name


if True:
    CONDITIONAL = 7              # inside a block: not read, and none exist here
'''


def submodules() -> dict[str, ModuleType]:
    """Every importable submodule of the package, bar the entry point."""
    return {
        info.name: importlib.import_module(f"capstack.{info.name}")
        for info in pkgutil.iter_modules(capstack.__path__)
        if info.name not in NOT_SURFACE
    }


def declared() -> dict[str, frozenset[str]]:
    """Each module's own declaration of what it makes public."""
    return {
        name: frozenset(module.__all__)
        for name, module in submodules().items()
        if getattr(module, "__all__", None) is not None
    }


def public_names_in(source: str) -> frozenset[str]:
    """The public top-level names a piece of module source defines.

    Separated from the file it usually reads so that it can be tested against
    source written for the purpose.
    """
    found: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        found.update(name for name in names if not name.startswith("_"))
    return frozenset(found)


def public_definitions(module: str) -> frozenset[str]:
    """Every public name a module defines at its top level, read from source."""
    source = Path(__file__).resolve().parent.parent / "src" / "capstack" / f"{module}.py"
    return public_names_in(source.read_text(encoding="utf-8"))


def package_types_in(hint: object) -> set[type]:
    """Every capstack class mentioned anywhere inside a type annotation."""
    found: set[type] = set()
    if isinstance(hint, type) and hint.__module__.startswith("capstack."):
        found.add(hint)
    origin = typing.get_origin(hint)
    if isinstance(origin, type) and origin.__module__.startswith("capstack."):
        found.add(origin)
    for argument in typing.get_args(hint):
        found |= package_types_in(argument)
    return found


def root_callables(root: frozenset[str]) -> Iterator[tuple[str, Callable[..., object]]]:
    """Each root function, and each public method or property of each root class."""
    for name in sorted(root):
        obj = getattr(capstack, name)
        if inspect.isfunction(obj):
            yield name, obj
        elif inspect.isclass(obj):
            for attribute, value in vars(obj).items():
                if attribute.startswith("_") and attribute != "__init__":
                    continue
                if isinstance(value, classmethod | staticmethod):
                    value = value.__func__
                if isinstance(value, property):
                    value = value.fget
                if inspect.isfunction(value):
                    yield f"{name}.{attribute}", value


def unreachable_signature_types(root: frozenset[str]) -> dict[str, list[str]]:
    """Signatures reachable from ``root`` that mention a type ``root`` lacks."""
    unreachable: dict[str, list[str]] = {}
    for label, function in root_callables(root):
        hints = typing.get_type_hints(function)
        mentioned = set().union(*(package_types_in(hint) for hint in hints.values()))
        missing = sorted({kind.__name__ for kind in mentioned} - root)
        if missing:
            unreachable[label] = missing
    return unreachable


@pytest.fixture(scope="module")
def root() -> frozenset[str]:
    return frozenset(capstack.__all__)


def test_every_declared_name_reaches_the_root(root: frozenset[str]) -> None:
    """A name a module declares public is importable from the package.

    This is the check the defect needed. Thirty-four names failed it.
    """
    missing: dict[str, list[str]] = {}
    for module, names in declared().items():
        absent = sorted(names - root)
        if absent:
            missing[module] = absent
    assert not missing, f"declared public but not re-exported from capstack/__init__.py: {missing}"


def test_no_module_is_absent_from_the_root_entirely(root: frozenset[str]) -> None:
    """A module with no name at the root is reported as the module.

    The check above catches it too, as five loose names, which reads as five
    small omissions rather than the one missing import line it was.
    """
    absent = sorted(module for module, names in declared().items() if names and not (names & root))
    assert not absent, (
        "these modules declare public names and none of them is at the root, so "
        f"the module was never wired into capstack/__init__.py: {absent}"
    )


def test_a_type_in_a_reachable_signature_is_itself_reachable(root: frozenset[str]) -> None:
    """A caller who can call something can name what it takes and returns.

    This is the symptom, checked directly: ``Deal.fee_schedule`` was callable
    from the root and its ``FeeMethod`` argument and ``FeeSchedule`` result
    were not nameable from there. Seven signatures failed it: two returned a
    ``TrailingWindow`` and a third took one.
    """
    unreachable = unreachable_signature_types(root)
    assert not unreachable, (
        f"reachable from capstack, written in types capstack does not export: {unreachable}"
    )


def test_the_fee_and_levered_entry_points_are_nameable(root: frozenset[str]) -> None:
    """The two signatures that showed the defect, named, so a failure says so."""
    for name in ("FeeMethod", "FeeSchedule", "LeveredSolution", "TaxCircularityError"):
        assert name in root, (
            f"capstack.{name} is not importable from the package root, yet Deal "
            "takes, returns or raises it"
        )


def test_every_exported_name_is_actually_there(root: frozenset[str]) -> None:
    """Nothing in ``__all__`` is a name the package does not have."""
    phantom = sorted(name for name in root if not hasattr(capstack, name))
    assert not phantom, f"in capstack.__all__ but not attributes of it: {phantom}"


def test_the_root_exports_each_name_once() -> None:
    """``__all__`` does not repeat itself."""
    repeated = {name: count for name, count in Counter(capstack.__all__).items() if count > 1}
    assert not repeated, f"repeated in capstack.__all__: {repeated}"


def test_every_module_declares_something() -> None:
    """No module is exempt from the checks above by declaring nothing."""
    silent = sorted(
        name for name, module in submodules().items() if getattr(module, "__all__", None) is None
    )
    assert silent == sorted(NO_DUNDER_ALL), (
        f"modules carrying no __all__, and so exempt from every check here: "
        f"{silent}; pinned: {sorted(NO_DUNDER_ALL)}"
    )


def test_the_root_is_exactly_the_union_of_the_declarations(root: frozenset[str]) -> None:
    """Equality, not containment.

    The first check catches a name the root is missing. This also catches the
    other direction — a name the root offers that no module claims — which is
    what a hand-kept list accumulates: an entry left behind by a rename, or one
    exported from a module that stopped declaring it.
    """
    union = frozenset().union(*declared().values())
    assert root == union | ROOT_ONLY, (
        f"only at the root: {sorted(root - union - ROOT_ONLY)}; "
        f"only in a module: {sorted(union - root)}"
    )


def test_a_module_declares_every_public_name_it_defines() -> None:
    """``__all__`` is not a curated subset of what the module defines.

    This catches the *next* omission rather than the last one: a name added to
    a module and left out of its ``__all__`` fails here, at the module, instead
    of sitting outside every other check in this file. Thirty-seven names
    failed it.
    """
    undeclared: dict[str, list[str]] = {}
    for module, names in declared().items():
        missing = sorted(public_definitions(module) - names)
        if missing:
            undeclared[module] = missing
    assert not undeclared, (
        f"defined at a module's top level and absent from its own __all__: {undeclared}"
    )


def test_no_name_is_declared_by_two_modules() -> None:
    """One name, one owner.

    The root imports every declaration into one namespace, so two modules
    declaring the same name means one silently shadows the other at the root —
    and the equality above still holds, because a set does not count.
    """
    owners = Counter(name for names in declared().values() for name in names)
    shared = sorted(name for name, count in owners.items() if count > 1)
    assert not shared, f"declared public by more than one module: {shared}"


def test_the_probe_reads_each_kind_of_definition_and_skips_each_non_definition() -> None:
    """The probe, against source written for it.

    The package exercises only some of its branches — nothing in
    ``src/capstack`` has an ``async def`` or assigns two names in one statement
    — so those paths are carried here rather than by reading. The exclusions
    matter as much: a probe that counted imports would report every module as
    failing to declare ``Decimal``; one that counted names in function bodies
    would report ``local_name``; one that counted ``__all__`` would have every
    module failing to declare its own declaration.
    """
    found = public_names_in(PROBE_SOURCE)
    assert found == {
        "LABEL",
        "BRACKET",
        "ANNOTATED",
        "PAIR_ONE",
        "PAIR_TWO",
        "public_function",
        "public_coroutine",
        "PublicClass",
    }, f"the probe read {sorted(found)}"
    for absent in ("math", "Decimal", "_PRIVATE", "__all__", "local_name", "attribute"):
        assert absent not in found, f"the probe counted {absent!r} as a definition"


def test_no_module_defines_a_public_name_inside_a_block() -> None:
    """The probe reads top-level statements only, so that is all there may be.

    A name assigned under a top-level ``if`` or ``try`` is a module attribute
    the probe does not see, which would exempt it from the declaration check
    without anyone choosing that. There are none; this keeps it that way.
    """
    hidden: dict[str, list[str]] = {}
    for module in declared():
        source = Path(__file__).resolve().parent.parent / "src" / "capstack" / f"{module}.py"
        names: set[str] = set()
        for node in ast.parse(source.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
                names.update(
                    inner.id
                    for inner in ast.walk(node)
                    if isinstance(inner, ast.Name)
                    and isinstance(inner.ctx, ast.Store)
                    and not inner.id.startswith("_")
                )
        if names:
            hidden[module] = sorted(names)
    assert not hidden, f"public names bound inside a top-level block: {hidden}"


def test_the_probe_reads_a_surface_worth_checking(root: frozenset[str]) -> None:
    """The checks above are not passing because they found nothing.

    Every assertion here is satisfied by an empty set, so a probe that stops
    importing modules, stops finding ``__all__`` or stops reading annotations
    turns the whole file green. Loose bounds: the claim is that a surface was
    read at all. ``fees`` is named because it is the module the defect centred
    on.
    """
    modules = declared()
    assert len(modules) >= 15, f"only {len(modules)} modules declared an __all__"
    assert len(root) >= 90, f"the root surface came back as {len(root)} names"
    assert "fees" in modules, "the fees module was not read"
    assert len(list(root_callables(root))) >= 400, "too few signatures were read"
    hints = typing.get_type_hints(capstack.Deal.fee_schedule)
    assert {kind.__name__ for kind in package_types_in(hints["return"])} == {"FeeSchedule"}


def test_the_annotation_walk_finds_types_nested_inside_others() -> None:
    """``package_types_in`` sees through unions, generics and tuples.

    The package's own signatures are mostly bare classes, so the nested branches
    are exercised here against annotations built for the purpose, with the
    classes imported from their modules so that this tests the walk and not the
    root. The exclusion
    matters too: a walk that counted ``Decimal`` or ``int`` would report every
    signature as unreachable.
    """
    nested = typing.Optional[tuple[list[FeeSchedule], dict[str, Deal]]]  # noqa: UP045
    assert package_types_in(nested) == {FeeSchedule, Deal}
    assert package_types_in(FeeMethod | None) == {FeeMethod}
    assert package_types_in(int | str | list[float]) == set()
