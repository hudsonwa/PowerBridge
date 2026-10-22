# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__version__ = "0.7.0"

# Let `import pl_transpiler._accel` (the optional Rust module) find an installed
# copy even when this source checkout shadows site-packages on sys.path, e.g.
# when you run from the repository root after `pip install` of the accel wheel.
from pkgutil import extend_path as _extend_path
from typing import TYPE_CHECKING as _TYPE_CHECKING
from typing import Any as _Any
__path__ = _extend_path(__path__, __name__)

# The public names below are imported on first use rather than here, so
# `python -m pl_transpiler.<module>` (catalog, tools) runs without the package
# having already imported that module (runpy would warn about it).
_LAZY = {
    'tokenise': 'lexer',
    'parse': 'parser',
    'generate': 'codegen',
    'UnimplementedKeywordError': 'errors',
    'restore_spelling': 'errors',
    'unimplemented_message': 'errors',
    'run_el': 'tools.pl_run',
    'load_bars_csv': 'tools.pl_run',
    'load_bars': 'tools.pl_run',
    'run_oracle': 'tools.pl_oracle',
    'get_config': 'runtime.instrument_config',
    'load_config_file': 'runtime.instrument_config',
    'catalog': '',
}

# The documented public API (docs/API.md).
__all__ = [
    'transpile',
    'run_el',
    'run_oracle',
    'load_bars_csv',
    'load_bars',
    'get_config',
    'load_config_file',
    'catalog',
    'UnimplementedKeywordError',
]

if _TYPE_CHECKING:
    from . import catalog as catalog
    from .errors import UnimplementedKeywordError as UnimplementedKeywordError
    from .tools.pl_oracle import run_oracle as run_oracle
    from .tools.pl_run import load_bars as load_bars
    from .tools.pl_run import load_bars_csv as load_bars_csv
    from .tools.pl_run import run_el as run_el
    from .runtime.instrument_config import get_config as get_config
    from .runtime.instrument_config import load_config_file as load_config_file


def __getattr__(name: str) -> "_Any":
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    if not module:  # a submodule, e.g. catalog
        value = import_module(f'.{name}', __name__)
    else:
        value = getattr(import_module(f'.{module}', __name__), name)
    globals()[name] = value
    return value


def __dir__() -> "list[str]":
    return sorted(set(globals()) | set(_LAZY))

def transpile(pl_source: str, trace: bool = False, partial: bool = False,
              persist: "bool | None" = None, dialect: str = "runnable") -> str:
    """Convert PowerLanguage source string to Python source string.

    dialect="runnable" (default) emits the code pl_transpile and pl_run execute:
    EL Variables live in a cross-bar `_state` dict, so the result can be driven
    bar by bar. dialect="mirror" emits the clean, stateless dialect that
    py_front reads back (Vars reset every bar; not for execution).
    If trace=True, generated code captures per-bar variable values into a _trace dict.
    persist: cross-bar `_state` container. None (default) follows the dialect;
    persist=False is a legacy spelling of dialect="mirror".
    If partial=True (opt-in), unimplemented EL keywords become execution-time
    stubs under a NOT-FAITHFUL watermark instead of raising at transpile time
    (the strict default)."""
    if dialect == "mirror":
        if trace or persist:
            raise ValueError("dialect='mirror' cannot be combined with trace or persist")
        persist = False
    elif dialect != "runnable":
        raise ValueError(f"unknown dialect {dialect!r}; expected 'runnable' or 'mirror'")
    elif persist is None and not trace:
        persist = True
    from .lexer import tokenise
    from .parser import parse
    from .codegen import generate
    from .errors import UnimplementedKeywordError, restore_spelling, unimplemented_message
    tokens, positions = tokenise(pl_source)
    ast = parse(tokens, positions=positions, source=pl_source)
    try:
        out = generate(ast, trace=trace, partial=partial, persist=persist)
    except UnimplementedKeywordError as exc:
        if not any(e.get('kind') for e in exc.errors):
            raise
        restore_spelling(exc.errors, pl_source)
        raise UnimplementedKeywordError(unimplemented_message(exc.errors),
                                        errors=exc.errors) from None
    return out
