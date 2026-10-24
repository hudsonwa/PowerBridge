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
"""Every name in pl_transpiler.__all__ (documented in docs/API.md) imports,
and the signatures docs/API.md shows match the code."""

import ast
import importlib
import inspect
import os
import re
import subprocess
import sys

import pl_transpiler


def test_all_names_import():
    for name in pl_transpiler.__all__:
        assert getattr(pl_transpiler, name) is not None, name


def test_from_import_star_in_fresh_interpreter():
    code = ("from pl_transpiler import *; import pl_transpiler as p; "
            "assert all(n in globals() for n in p.__all__)")
    subprocess.run([sys.executable, '-c', code], check=True)


def test_catalog_is_the_submodule():
    assert pl_transpiler.catalog is importlib.import_module('pl_transpiler.catalog')


# --- docs/API.md signatures match the code ---------------------------------

_API_MD = os.path.join(os.path.dirname(__file__), '..', 'docs', 'API.md')
_DOCUMENTED = ('transpile', 'run_el', 'run_oracle', 'load_bars_csv',
               'load_bars', 'get_config', 'load_config_file')


def _doc_signatures():
    """{name: ast.arguments} for each signature in API.md's ```python
    blocks that directly follow a `### ` heading."""
    text = open(_API_MD, encoding='utf-8').read()
    sigs = {}
    for block in re.findall(r'^### [^\n]*\n\n```python\n(.*?)^```', text,
                            re.M | re.S):
        for m in re.finditer(r'^(\w+)\((.*?)\)(?: -> [^\n]*)?$', block,
                             re.M | re.S):
            tree = ast.parse(f"def {m.group(1)}({m.group(2)}): pass")
            sigs[m.group(1)] = tree.body[0].args
    return sigs


def _doc_params(args):
    """[(name, kind, default)] in signature order."""
    P = inspect.Parameter
    pos = [(a.arg, P.POSITIONAL_OR_KEYWORD) for a in args.args]
    pos_defaults = [inspect.Parameter.empty] * (len(pos) - len(args.defaults))
    pos_defaults += [ast.literal_eval(d) for d in args.defaults]
    out = [(n, k, d) for (n, k), d in zip(pos, pos_defaults)]
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out.append((a.arg, P.KEYWORD_ONLY,
                    P.empty if d is None else ast.literal_eval(d)))
    return out


def test_api_md_signatures_match_code():
    sigs = _doc_signatures()
    for name in _DOCUMENTED:
        assert name in sigs, f"docs/API.md has no signature for {name}"
        code = [(p.name, p.kind, p.default) for p in
                inspect.signature(getattr(pl_transpiler, name)).parameters.values()]
        assert _doc_params(sigs[name]) == code, f"docs/API.md {name} signature drifted"


def test_public_signatures_have_no_private_parameters():
    for name in _DOCUMENTED:
        params = inspect.signature(getattr(pl_transpiler, name)).parameters
        assert not [p for p in params if p.startswith('_')], name
