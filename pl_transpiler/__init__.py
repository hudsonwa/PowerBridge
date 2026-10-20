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

__version__ = "0.2.0"

# Let `import pl_transpiler._accel` (the optional Rust module) find an installed
# copy even when this source checkout shadows site-packages on sys.path, e.g.
# when you run from the repository root after `pip install` of the accel wheel.
from pkgutil import extend_path as _extend_path
__path__ = _extend_path(__path__, __name__)

from .lexer import tokenise
from .parser import parse
from .codegen import generate

def transpile(pl_source: str, trace=False, partial=False, persist=None) -> str:
    """Convert PowerLanguage source string to Python source string.
    If trace=True, generated code captures per-bar variable values into a _trace dict.
    persist: cross-bar `_state` container. None (default) follows `trace`.
    persist=True with trace=False is the runnable no-trace dialect (`_state`
    lives, `_trace` does not). The default persist=False / trace=False path
    remains the clean mirror dialect that py_front reads back.
    If partial=True (opt-in), unimplemented EL keywords become execution-time
    stubs under a NOT-FAITHFUL watermark instead of raising at transpile time; the
    strict default (partial=False) stays byte-identical."""
    tokens, positions = tokenise(pl_source)
    ast = parse(tokens, positions=positions, source=pl_source)
    return generate(ast, trace=trace, partial=partial, persist=persist)
