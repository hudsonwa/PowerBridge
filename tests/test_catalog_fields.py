#!/usr/bin/env python3
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

"""test_catalog_fields.py — the generated evidence fields of pl_signatures.jsonl.

- The shipped jsonl equals tools/gen_catalog_fields.py output, byte for byte.
- Every record validates against pl_signatures.schema.json (a tiny validator for the
  subset of JSON Schema the file uses; no jsonschema dependency).
- catalog.arg_types / max_arity / verification and the coverage() level counts."""
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gen_catalog_fields  # noqa: E402
from pl_transpiler import catalog  # noqa: E402

_SIG_PATH = os.path.join(REPO, "pl_transpiler", "tools", "pl_signatures.jsonl")
_SCHEMA_PATH = os.path.join(REPO, "pl_transpiler", "tools", "pl_signatures.schema.json")
_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "null": type(None),
}


def _records():
    with open(_SIG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _is_type(value, name):
    if name == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def _validate(value, schema, path="$"):
    """Return a list of error strings for ``value`` against ``schema``."""
    errs = []
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_is_type(value, t) for t in types):
            return [f"{path}: {value!r} is not of type {types}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errs.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < {schema['minimum']}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += _validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing {key!r}")
        for key, item in value.items():
            if key in props:
                errs += _validate(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected {key!r}")
    return errs


def test_shipped_jsonl_matches_generator():
    with open(_SIG_PATH, encoding="utf-8") as f:
        shipped = f.read()
    # The generator reads EL embedded in tests/, so editing test EL can make this fail;
    # that is expected: regenerate and commit the jsonl.
    assert shipped == gen_catalog_fields.build(), (
        "pl_signatures.jsonl is stale (tests/ EL or a PowerBridge arity table changed). "
        f"Regenerate it from the repo root with: {gen_catalog_fields.REGEN_CMD}")


def test_every_record_matches_schema():
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    recs = _records()
    assert len(recs) == catalog.coverage()["signatures"]
    errs = []
    for rec in recs:
        errs += _validate(rec, schema, rec.get("keyword", "?"))
        if rec["max_arity"] is not None:
            assert rec["max_arity"] >= rec["min_arity"], rec
        if rec["arg_types"] is not None:
            assert len(rec["arg_types"]) == rec["max_arity"], rec
        assert (rec["verified_by"] is None) == (
            rec["verification"] in ("documented-unverified", "n/a")), rec
        assert (rec["kind"] == "syntax") == (rec["verification"] == "n/a"), rec
        if rec["kind"] == "syntax":
            assert rec["max_arity"] is None and rec["arg_types"] is None, rec
    assert errs == [], errs[:20]


def test_validator_rejects_bad_records():
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    good = dict(_records()[0])
    assert _validate(good, schema) == []
    assert _validate(dict(good, verification="maybe"), schema)
    assert _validate(dict(good, verification="verified:capture"), schema)
    assert _validate(dict(good, kind="word"), schema)
    assert _validate(dict(good, max_arity=True), schema)
    assert _validate(dict(good, arg_types=["float"]), schema)
    assert _validate(dict(good, extra=1), schema)
    assert _validate({k: v for k, v in good.items() if k != "keyword"}, schema)


def test_field_accessors():
    # Power lowers to a numeric math intrinsic and the semantic pass checks 2 args.
    assert catalog.max_arity("Power") == 2
    assert catalog.arg_types("power") == ["numeric", "numeric"]
    # TL_New's runtime function takes six fixed positional arguments of unchecked type.
    assert catalog.max_arity("TL_New") == 6
    assert catalog.arg_types("TL_New") == ["any"] * 6
    # A runtime *args stub establishes nothing.
    assert catalog.max_arity("Arw_GetDate") is None
    assert catalog.arg_types("Arw_GetDate") is None
    # A codegen lowering that only accepts exact counts defines the arity.
    assert catalog.max_arity("TimeToString") == 1
    # A risk statement is parsed as a statement, so codegen's expression lowering
    # does not define its arity.
    assert catalog.max_arity("SetStopPosition") is None
    assert catalog.verification("CurrentDate") == "verified:capture-strategy"
    # Syntax words are never reported as verified.
    for word in ("If", "Then", "Begin", "Shares", "Ago"):
        assert catalog.kind(word) == "syntax", word
        assert catalog.verification(word) == "n/a", word
    assert catalog.kind("CurrentDate") == "builtin"
    assert catalog.verification("TL_New") == "verified:test"
    for fn in (catalog.arg_types, catalog.max_arity, catalog.verification, catalog.kind):
        try:
            fn("no_such_keyword_xyz")
        except KeyError as e:
            assert "signature catalog" in str(e)
        else:
            raise AssertionError(f"{fn.__name__} must raise KeyError on unknown name")


def test_coverage_verification_counts():
    counts = collections.Counter(r["verification"] for r in _records())
    cov = catalog.coverage()
    for level in catalog.VERIFICATION_LEVELS:
        assert cov[f"verification:{level}"] == counts[level], (level, cov)
    assert sum(cov[f"verification:{lv}"] for lv in catalog.VERIFICATION_LEVELS) == cov["signatures"]
    kinds = collections.Counter(r["kind"] for r in _records())
    for k in catalog.KINDS:
        assert cov[f"kind:{k}"] == kinds[k], (k, cov)
    assert cov["with_max_arity"] == sum(r["max_arity"] is not None for r in _records())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  [PASS] {t.__name__}")
    print(f"\ntest_catalog_fields: {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
