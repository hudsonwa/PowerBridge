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

"""pl_transpiler/ir.py — the `program_ir` IR serialiser (ACCEL INTERFACE v2, step 1).

The EL dialect's parser already produces the shared dict AST (see parser.py). This
module makes that AST **serialisable, versioned and round-trip lossless** so a
foreign interpreter (the Rust engine) can consume it without embedding Python.

Public contract (the ACCEL v2 `program_ir` surface):

    IR_SCHEMA_VERSION : int
        Schema version stamped into every payload. Bump whenever the serialised
        shape or the node schema changes. A reader compares its own version
        against the payload's and REJECTS an unknown version explicitly
        (IrVersionError) rather than mis-parsing it.

    serialise_ir(ast) -> str
        Validate `ast` against the node schema — a compile-time refusal: any
        construct the dialect cannot express raises IrSchemaError, nothing is
        silently omitted — then produce a deterministic JSON payload:

            {"ir_version": 1, "kind": "pl_transpiler.ast", "ast": {...}}

        Deterministic: sorted keys, fixed separators, ensure_ascii, no timestamps,
        no dict-order dependence. Serialising the same AST twice yields identical
        bytes.

    deserialise_ir(payload) -> ast
        The inverse. JSON-decodes, checks the `kind`, requires
        `ir_version == IR_SCHEMA_VERSION` (else IrVersionError), re-validates the
        AST against the schema, and returns it.

    Errors
        IrError          base class for all IR failures
        IrSchemaError    node/record outside the schema (unknown node type,
                         unknown key, wrong value type) — compile-time refusal
        IrVersionError   payload schema version not supported by this reader

The schema enumerates every node type the parser can emit (35 node kinds) and
every record kind it nests (5: var/input/array decl records, switch cases, and
verbatim comments). The round-trip proof lives in tests/test_ir_roundtrip.py.
"""

import json

IR_SCHEMA_VERSION = 1
IR_KIND = "pl_transpiler.ast"


class IrError(Exception):
    """Base class for IR serialiser failures."""


class IrSchemaError(IrError):
    """A node/record is outside the enumerated dialect schema — a compile-time
    refusal. Nothing outside the schema is ever silently encoded."""


class IrVersionError(IrError):
    """The payload's schema version is not supported by this reader."""


# ---------------------------------------------------------------------------
# Node schema — every 'type' the parser emits, with its allowed keys and the
# value kind of each key. Kinds:
#   'str' / 'int' / 'bool'      exact scalar type
#   'node'                      required nested node dict
#   'node?'                     optional nested node dict (None allowed)
#   'mixed'                     node dict, or scalar (str/int/bool/None)
#   'list[node]'                list of nested node dicts
#   'list[node]?'               None or a list of nested node dicts
#   'list[str]'                 list of strings
#   'list[rec:X]'               list of nested records of kind X
#   'rec:comment'               a single verbatim-comment record
# ---------------------------------------------------------------------------

_NODE_SCHEMA = {
    'program': {'body': 'list[node]'},
    'var_decl': {'decls': 'list[rec:var_decl]'},
    'input_decl': {'decls': 'list[rec:input_decl]'},
    'array_decl': {'decls': 'list[rec:array_decl]'},
    'if': {'cond': 'node', 'body': 'list[node]', 'else_body': 'list[node]?',
           'then_block': 'bool', 'else_block': 'bool'},
    'for': {'var': 'str', 'start': 'node', 'end': 'node', 'downto': 'bool',
            'body': 'list[node]', 'block': 'bool'},
    'while': {'cond': 'node', 'body': 'list[node]', 'block': 'bool'},
    'repeat_until': {'body': 'list[node]', 'cond': 'node'},
    'once': {'body': 'list[node]', 'block': 'bool'},
    'switch': {'expr': 'node', 'cases': 'list[rec:switch_case]',
               'default': 'list[node]?'},
    'attribute': {'name': 'str', 'value': 'node?', 'raw_args': 'str'},
    'commentary': {'msg': 'node'},
    'order': {'action': 'str', 'label': 'mixed', 'quantity': 'node?',
              'entry_label': 'node?', 'bar_timing': 'str', 'order_type': 'str',
              'price': 'node?', 'total': 'bool'},
    'risk': {'func': 'str', 'arg': 'node?', 'args': 'list[node]'},
    'plot': {'name': 'str', 'value': 'node', 'label': 'mixed',
             'extra_args': 'list[node]'},
    'alert': {'msg': 'node'},
    'print': {'args': 'list[node]'},
    'assign': {'target': 'node', 'value': 'node'},
    'expr_stmt': {'expr': 'node'},
    'break': {},
    'continue': {},
    'abort': {},
    'binop': {'op': 'str', 'left': 'node', 'right': 'node'},
    'unaryop': {'op': 'str', 'operand': 'node'},
    'compare': {'op': 'str', 'left': 'node', 'right': 'node'},
    'call': {'name': 'str', 'args': 'list[node]'},
    'number': {'value': 'str', '_symbol': 'str'},
    'string': {'value': 'str'},
    'boolean': {'value': 'bool'},
    'ident': {'name': 'str'},
    'bar_ref': {'series': 'node', 'index': 'node'},
    'position_kw': {'kwarg': 'str'},
    'position_kw_call': {'kwarg': 'str', 'args': 'list[node]'},
    'mc_kw': {'kwarg': 'str'},
    'data_of': {'series': 'str', 'data_num': 'int'},
}

# Additive parser metadata: underscore-prefixed keys that may ride on ANY node.
_NODE_META = {
    '_line': 'int', '_col': 'int', '_ident': 'str',
    '_symbol': 'str', '_var_ident': 'str',
}

# Program-root-only metadata.
_PROGRAM_META = {
    '_comments': 'list[rec:comment]',
    '_leading_blanks': 'int',
    '_warnings': 'list[str]',
}

# Nested records (dicts without a 'type'). A record's allowed keys.
_RECORD_SCHEMA = {
    'var_decl': {'name': 'str', 'init': 'node', 'data_ref': 'node?',
                 'intrabar_persist': 'bool'},
    'input_decl': {'name': 'str', 'default': 'node'},
    'array_decl': {'name': 'str', 'size': 'node?', 'init': 'node',
                   'intrabar_persist': 'bool'},
    'switch_case': {'value': 'node', 'body': 'list[node]'},
    'comment': {'text': 'str', 'line': 'int', 'col': 'int', 'trailing': 'bool'},
}

_RECORD_META = {'_ident': 'str'}

# Exhaustive enumeration of the dialect, for the "opcodes covered" gate and for a
# foreign reader to enumerate against. Derived from the parser (parser.py) and
# verified against the whole corpus by tests/test_ir_roundtrip.py.
IR_NODE_TYPES = frozenset(_NODE_SCHEMA)
IR_RECORD_KINDS = frozenset(_RECORD_SCHEMA)


def _path_str(path):
    return "$" + "".join(f"[{p}]" for p in path)


def _check_node(node, path):
    if not isinstance(node, dict):
        raise IrSchemaError(
            f"expected a node dict at {_path_str(path)}, "
            f"got {type(node).__name__}")
    ntype = node.get('type')
    if not isinstance(ntype, str) or not ntype:
        raise IrSchemaError(
            f"node at {_path_str(path)} has no valid 'type' — not part of the "
            f"dialect schema; refusing to encode")
    if ntype not in _NODE_SCHEMA:
        raise IrSchemaError(
            f"unknown node type {ntype!r} at {_path_str(path)} — not part of "
            f"the dialect schema; refusing to encode")
    allowed = set(_NODE_SCHEMA[ntype]) | set(_NODE_META)
    if ntype == 'program':
        allowed |= set(_PROGRAM_META)
    for key, value in node.items():
        if key == 'type':
            continue
        if key not in allowed:
            raise IrSchemaError(
                f"key {key!r} is not part of node type {ntype!r} at "
                f"{_path_str(path)} — refusing to encode")
        kind = (_NODE_SCHEMA[ntype].get(key)
                or _NODE_META.get(key) or _PROGRAM_META.get(key))
        _check_value(kind, value, path + [key])


def _check_record(which, rec, path):
    if not isinstance(rec, dict):
        raise IrSchemaError(
            f"expected a {which} record at {_path_str(path)}, "
            f"got {type(rec).__name__}")
    allowed = set(_RECORD_SCHEMA[which]) | set(_RECORD_META)
    for key, value in rec.items():
        if key not in allowed:
            raise IrSchemaError(
                f"key {key!r} is not part of a {which} record at "
                f"{_path_str(path)} — refusing to encode")
        kind = _RECORD_SCHEMA[which].get(key) or _RECORD_META.get(key)
        _check_value(kind, value, path + [key])


def _check_value(kind, value, path):
    if kind == 'node':
        if value is None:
            raise IrSchemaError(
                f"expected a node at {_path_str(path)} but got None")
        _check_node(value, path)
    elif kind == 'node?':
        if value is not None:
            _check_node(value, path)
    elif kind == 'mixed':
        if isinstance(value, dict):
            _check_node(value, path)
        elif isinstance(value, (str, int, bool)) or value is None:
            pass
        else:
            raise IrSchemaError(
                f"expected a node or scalar at {_path_str(path)}, "
                f"got {type(value).__name__}")
    elif kind == 'list[node]':
        if not isinstance(value, list):
            raise IrSchemaError(
                f"expected a list at {_path_str(path)}, got {type(value).__name__}")
        for i, item in enumerate(value):
            _check_node(item, path + [i])
    elif kind == 'list[node]?':
        if value is not None:
            if not isinstance(value, list):
                raise IrSchemaError(
                    f"expected a list or None at {_path_str(path)}, "
                    f"got {type(value).__name__}")
            for i, item in enumerate(value):
                _check_node(item, path + [i])
    elif kind == 'list[str]':
        if (not isinstance(value, list)
                or not all(isinstance(x, str) for x in value)):
            raise IrSchemaError(
                f"expected a list of strings at {_path_str(path)}")
    elif kind.startswith('list[rec:'):
        which = kind[len('list[rec:'):-1]
        if not isinstance(value, list):
            raise IrSchemaError(
                f"expected a list at {_path_str(path)}, got {type(value).__name__}")
        for i, item in enumerate(value):
            _check_record(which, item, path + [i])
    elif kind == 'rec:comment':
        _check_record('comment', value, path)
    elif kind == 'str':
        if not isinstance(value, str):
            raise IrSchemaError(
                f"expected a string at {_path_str(path)}, "
                f"got {type(value).__name__}")
    elif kind == 'int':
        if isinstance(value, bool) or not isinstance(value, int):
            raise IrSchemaError(
                f"expected an int at {_path_str(path)}, "
                f"got {type(value).__name__}")
    elif kind == 'bool':
        if not isinstance(value, bool):
            raise IrSchemaError(
                f"expected a bool at {_path_str(path)}, "
                f"got {type(value).__name__}")
    else:
        raise AssertionError(f"internal: unknown schema kind {kind!r}")


def serialise_ir(ast, *, version=None):
    """Validate `ast` against the dialect schema and encode it deterministically.

    Raises IrSchemaError for anything the dialect cannot express (compile-time
    refusal) and IrVersionError if `version` is not the current schema version.
    """
    if version is None:
        version = IR_SCHEMA_VERSION
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != IR_SCHEMA_VERSION):
        raise IrVersionError(
            f"cannot serialise with schema version {version!r}; this build "
            f"writes version {IR_SCHEMA_VERSION}")
    _check_node(ast, [])
    if ast.get('type') != 'program':
        raise IrSchemaError(
            f"IR root must be a 'program' node, got {ast.get('type')!r}")
    payload = {'ir_version': version, 'kind': IR_KIND, 'ast': ast}
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True)


def deserialise_ir(payload, *, expected_version=None):
    """Decode and validate an IR payload back into the dict AST.

    Raises IrVersionError when the payload's schema version is not supported,
    IrSchemaError when the embedded AST violates the schema, and IrError for a
    structurally malformed payload.
    """
    if expected_version is None:
        expected_version = IR_SCHEMA_VERSION
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8')
    if not isinstance(payload, str):
        raise IrError(
            f"expected IR payload as str or bytes, got {type(payload).__name__}")
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise IrError(f"malformed IR payload: {exc}") from exc
    if not isinstance(data, dict):
        raise IrError("malformed IR payload: expected a JSON object")
    kind = data.get('kind')
    if kind != IR_KIND:
        raise IrError(f"IR payload kind {kind!r} is not {IR_KIND!r}")
    version = data.get('ir_version')
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != expected_version):
        raise IrVersionError(
            f"unsupported IR schema version {version!r}; this reader supports "
            f"{expected_version}")
    ast = data.get('ast')
    if ast is None:
        raise IrError("malformed IR payload: missing 'ast'")
    _check_node(ast, [])
    if ast.get('type') != 'program':
        raise IrSchemaError(
            f"IR root must be a 'program' node, got {ast.get('type')!r}")
    return ast
