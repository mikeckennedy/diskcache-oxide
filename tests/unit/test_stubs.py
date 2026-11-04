"""`_core.pyi` is hand-written, so these keep it honest against the compiled module."""

from __future__ import annotations

import ast
import inspect
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
    return [p for p in params if p[0] not in ('self', 'cls')]


def _check_defaults(where: str, stub: Function, runtime: inspect.Signature) -> None:
    actual = [p for p in runtime.parameters.values() if p.name not in ('self', 'cls')]
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


def test_the_stub_lists_what_the_module_exports() -> None:
    assert _literal_all() == list(_core.__all__)


@pytest.mark.parametrize('name', list(_core.__all__))
def test_every_export_is_defined_in_the_stub(name: str) -> None:
    assert name in CLASSES or name in FUNCTIONS or name in CONSTANTS, f'{name} is missing from _core.pyi'


@pytest.mark.parametrize('name', sorted(FUNCTIONS))
def test_function_signatures_match(name: str) -> None:
    _check_defaults(name, FUNCTIONS[name], inspect.signature(getattr(_core, name)))


@pytest.mark.parametrize('name', sorted(n for n, c in CLASSES.items() if n != 'Cache'))
def test_exception_bases_match(name: str) -> None:
    node = CLASSES[name]
    runtime = getattr(_core, name)
    bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
    assert bases == [b.__name__ for b in runtime.__bases__], name


def test_the_constructor_matches() -> None:
    _check_defaults(
        'Cache',
        _members(CLASSES['Cache'])['__new__'][0],
        inspect.signature(_core.Cache),
    )


CACHE_MEMBERS = _members(CLASSES['Cache'])


@pytest.mark.parametrize('name', sorted(CACHE_MEMBERS))
def test_every_stubbed_member_exists(name: str) -> None:
    assert hasattr(_core.Cache, name), f'Cache.{name} is in the stub but not the module'


# The garbage collector's protocol, not part of the API.
RUNTIME_ONLY = {'__traverse__', '__clear__'}


@pytest.mark.parametrize(
    'name',
    sorted(
        n
        for n, m in vars(_core.Cache).items()
        if callable(m) or isinstance(m, property) or inspect.isgetsetdescriptor(m)
    ),
)
def test_every_public_member_is_stubbed(name: str) -> None:
    public = not name.startswith('_') or name.startswith('__') and name not in RUNTIME_ONLY
    # Whatever `object` already declares (`__repr__`, `__new__`, ...) needs no stub line.
    if not public or hasattr(object, name):
        return
    assert name in CACHE_MEMBERS, f'Cache.{name} is in the module but not the stub'


@pytest.mark.parametrize(
    'name',
    sorted(
        n
        for n, defs in CACHE_MEMBERS.items()
        if not n.startswith('__') and not _is_property(defs[0]) and not n.startswith('_')
    ),
)
def test_method_signatures_match(name: str) -> None:
    runtime = inspect.signature(getattr(_core.Cache, name))
    for definition in CACHE_MEMBERS[name]:
        expected = [(n, k) for n, k, _ in _signature(definition)]
        assert expected == [(p.name, p.kind) for p in runtime.parameters.values() if p.name != 'self'], f'Cache.{name}'
    if len(CACHE_MEMBERS[name]) == 1:
        _check_defaults(f'Cache.{name}', CACHE_MEMBERS[name][0], runtime)
