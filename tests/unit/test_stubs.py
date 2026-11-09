"""`_core.pyi` is hand-written, so these keep it honest against the compiled module."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import diskcache_oxide
import pytest
from diskcache_oxide import _core

STUB = ast.parse((Path(diskcache_oxide.__file__).parent / '_core.pyi').read_text())
Function = ast.FunctionDef | ast.AsyncFunctionDef


def _top(kind: Any) -> dict[str, Any]:
    return {node.name: node for node in STUB.body if isinstance(node, kind)}


CLASSES: dict[str, ast.ClassDef] = _top(ast.ClassDef)
FUNCTIONS: dict[str, ast.FunctionDef] = _top(ast.FunctionDef)
CONSTANTS = {
    target.id
    for node in STUB.body
    if isinstance(node, ast.Assign | ast.AnnAssign)
    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    if isinstance(target, ast.Name)
}


def _literal_all() -> list[str]:
    for node in STUB.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('the stub has no __all__')


def _members(cls: ast.ClassDef) -> dict[str, list[Function]]:
    """Name -> definitions (several for an `@overload`)."""
    found: dict[str, list[Function]] = {}
    for node in cls.body:
        if isinstance(node, Function):
            found.setdefault(node.name, []).append(node)
    return found


def _is_property(node: Function) -> bool:
    return any(isinstance(d, ast.Name) and d.id == 'property' for d in node.decorator_list)


def _signature(node: Function) -> list[tuple[str, inspect._ParameterKind, ast.expr | None]]:
    """(name, kind, default) from a stub definition. `...` and non-literal defaults are `inspect._empty`-free
    markers."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    params: list[tuple[str, inspect._ParameterKind, ast.expr | None]] = []
    for index, (arg, default) in enumerate(zip(positional, defaults, strict=True)):
        kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        params.append((arg.arg, kind, default))
    params.extend(
        (arg.arg, inspect.Parameter.KEYWORD_ONLY, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    return _without_receiver(params, lambda p: p[0])


def _without_receiver[T](params: list[T], name: Callable[[T], str]) -> list[T]:
    """Drops a leading `self` or `cls` (the receiver), and only that one: `TypeRegistry.register(cls, ...)`
    takes a class as its first real parameter."""
    return params[1:] if params and name(params[0]) in ('self', 'cls') else params


def _check_defaults(where: str, stub: Function, runtime: inspect.Signature) -> None:
    actual = _without_receiver(list(runtime.parameters.values()), lambda p: p.name)
    expected = _signature(stub)
    assert [(n, k.name) for n, k, _ in expected] == [(p.name, p.kind.name) for p in actual], where
    for (name, _, default), param in zip(expected, actual, strict=True):
        where_param = f'{where}({name})'
        if default is None:
            assert param.default is inspect.Parameter.empty, f'{where_param}: the stub has no default'
            continue
        assert param.default is not inspect.Parameter.empty, f'{where_param}: the runtime has no default'
        value = (
            ast.literal_eval(default) if not (isinstance(default, ast.Constant) and default.value is Ellipsis) else ...
        )
        if value is not ...:
            assert param.default == value, where_param


# In a coverage build only (`cfg(coverage)`, scripts/coverage.sh), never in a wheel: not in the stub.
COVERAGE_ONLY = {'_write_coverage'}
EXPORTS = [name for name in _core.__all__ if name not in COVERAGE_ONLY]


def test_the_stub_lists_what_the_module_exports() -> None:
    assert _literal_all() == EXPORTS


@pytest.mark.parametrize('name', EXPORTS)
def test_every_export_is_defined_in_the_stub(name: str) -> None:
    assert name in CLASSES or name in FUNCTIONS or name in CONSTANTS, f'{name} is missing from _core.pyi'


@pytest.mark.parametrize('name', sorted(FUNCTIONS))
def test_function_signatures_match(name: str) -> None:
    _check_defaults(name, FUNCTIONS[name], inspect.signature(getattr(_core, name)))


CACHE_CLASSES = ('Cache', 'FanoutCache', 'TypeRegistry')


@pytest.mark.parametrize(
    'name', sorted(n for n, c in CLASSES.items() if n not in ('Cache', 'FanoutCache', 'Transaction', 'TypeRegistry'))
)
def test_exception_bases_match(name: str) -> None:
    node = CLASSES[name]
    runtime = getattr(_core, name)
    bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
    assert bases == [b.__name__ for b in runtime.__bases__], name


@pytest.mark.parametrize('cls', CACHE_CLASSES)
def test_the_constructor_matches(cls: str) -> None:
    _check_defaults(cls, _members(CLASSES[cls])['__new__'][0], inspect.signature(getattr(_core, cls)))


MEMBERS = {cls: _members(CLASSES[cls]) for cls in CACHE_CLASSES}


@pytest.mark.parametrize(('cls', 'name'), [(c, n) for c in CACHE_CLASSES for n in sorted(MEMBERS[c])])
def test_every_stubbed_member_exists(cls: str, name: str) -> None:
    assert hasattr(getattr(_core, cls), name), f'{cls}.{name} is in the stub but not the module'


# The garbage collector's protocol, not part of the API.
RUNTIME_ONLY = {'__traverse__', '__clear__'}


@pytest.mark.parametrize(
    ('cls', 'name'),
    [
        (c, n)
        for c in CACHE_CLASSES
        for n, m in sorted(vars(getattr(_core, c)).items())
        if callable(m) or isinstance(m, property) or inspect.isgetsetdescriptor(m)
    ],
)
def test_every_public_member_is_stubbed(cls: str, name: str) -> None:
    public = not name.startswith('_') or name.startswith('__') and name not in RUNTIME_ONLY
    # Whatever `object` already declares (`__repr__`, `__new__`, ...) needs no stub line.
    if not public or hasattr(object, name):
        return
    assert name in MEMBERS[cls], f'{cls}.{name} is in the module but not the stub'


@pytest.mark.parametrize(
    ('cls', 'name'),
    [
        (c, n)
        for c in CACHE_CLASSES
        for n, defs in sorted(MEMBERS[c].items())
        if not n.startswith('__') and not _is_property(defs[0]) and not n.startswith('_')
    ],
)
def test_method_signatures_match(cls: str, name: str) -> None:
    runtime = inspect.signature(getattr(getattr(_core, cls), name))
    for definition in MEMBERS[cls][name]:
        expected = [(n, k) for n, k, _ in _signature(definition)]
        actual = _without_receiver(list(runtime.parameters.values()), lambda p: p.name)
        assert expected == [(p.name, p.kind) for p in actual], f'{cls}.{name}'
    if len(MEMBERS[cls][name]) == 1:
        _check_defaults(f'{cls}.{name}', MEMBERS[cls][name][0], runtime)
