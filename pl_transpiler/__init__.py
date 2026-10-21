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

__version__ = "0.5.0"

# Let `import pl_transpiler._accel` (the optional Rust module) find an installed
# copy even when this source checkout shadows site-packages on sys.path, e.g.
# when you run from the repository root after `pip install` of the accel wheel.
from pkgutil import extend_path as _extend_path
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
}


def __getattr__(name: str) -> "_Any":
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(f'.{module}', __name__), name)
    globals()[name] = value
    return value


def __dir__() -> "list[str]":
    return sorted(set(globals()) | set(_LAZY))

def transpile(pl_source: str, trace: bool = False, partial: bool = False,
              persist: "bool | None" = None) -> str:
    """Convert PowerLanguage source string to Python source string.
    If trace=True, generated code captures per-bar variable values into a _trace dict.
    persist: cross-bar `_state` container. None (default) follows `trace`.
    persist=True with trace=False is the runnable no-trace dialect (`_state`
    lives, `_trace` does not). The default persist=False / trace=False path
    remains the clean mirror dialect that py_front reads back.
    If partial=True (opt-in), unimplemented EL keywords become execution-time
    stubs under a NOT-FAITHFUL watermark instead of raising at transpile time
    (the strict default)."""
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
