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

The last check is the one that names the symptom rather than the cause: a type
that appears in the signature of anything reachable from the root is itself
reachable from the root. Seven signatures failed it.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from collections import Counter
from collections.abc import Callable, Iterator
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
#: assigned in ``__init__.py`` itself. ``rescale`` is defined in ``money`` and
#: exported from the root, but ``money.__all__`` omits it: pinned here so that
#: the omission is visible rather than tolerated.
ROOT_ONLY = frozenset({"__version__", "rescale"})


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


def test_the_root_holds_only_declared_names_and_the_pinned_exceptions(
    root: frozenset[str],
) -> None:
    """Every root name is claimed by a module, bar the pinned set.

    Containment in the other direction: an entry the root offers that no module
    declares is what a hand-kept list accumulates after a rename.
    """
    union = frozenset().union(*declared().values())
    assert root - union == ROOT_ONLY, f"at the root and declared by no module: {sorted(root - union)}"


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
