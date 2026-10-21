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

"""gen_reference.py — build the keyword reference pages in docs/reference/.

Facts come from the catalog (``pl_transpiler.catalog``: kind, return type, arity,
argument types, verification level) and from running PowerBridge itself. The
descriptions and examples come from the hand-written ``docs/reference/_notes.json``.

Entries: every catalog builtin that is on the MultiCharts compile list
(``mc_verified_builtins.txt``) or whose verification level is ``verified:test`` or
``verified:capture-strategy``. ``_notes.json`` must describe exactly that set.

Every entry's example (or, without one, a minimal call built from its arity) is run
with ``run_el`` over the first bars of ``examples/NQ_sample_bars.csv``. The outcome
is shown as the entry's run status. An example that fails to transpile, or fails at
run time for any reason other than an unimplemented runtime builtin, stops the build.

Usage:  python tools/gen_reference.py [--check]
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pl_transpiler import catalog, get_config, load_bars_csv, run_el  # noqa: E402
from pl_transpiler.runtime import _rt_core  # noqa: E402

REF_DIR = os.path.join(REPO, "docs", "reference")
NOTES_PATH = os.path.join(REF_DIR, "_notes.json")
BARS_PATH = os.path.join(REPO, "examples", "NQ_sample_bars.csv")
N_BARS = 40
REGEN_CMD = "python tools/gen_reference.py"

LEVEL_TEXT = {
    "verified:capture-strategy": "capture (strategy-level)",
    "verified:test": "PowerBridge test",
    "documented-unverified": "unverified",
}
STATUS_RUNS = "runs"
STATUS_NOOP = "accepted; no effect in a backtest"
STATUS_STOPS = "transpiles; the run stops (not implemented in the runtime)"


def selected():
    """Catalog records the reference covers, sorted case-insensitively."""
    recs = catalog._load_signatures()
    listed = catalog._load_verified_builtins()
    out = [r for low, r in recs.items() if r["kind"] == "builtin" and (
        low in listed or r["verification"] in ("verified:test", "verified:capture-strategy"))]
    return sorted(out, key=lambda r: r["keyword"].lower())


def probe_source(rec):
    """A minimal EL program that calls ``rec``'s keyword with placeholder arguments."""
    name, n = rec["keyword"], rec["min_arity"]
    args = ["1"] * n
    if n and (name.lower().startswith("array_") or name.lower() == "fill_array"):
        args[0] = "A"
    call = f"{name}({', '.join(args)})" if n else name
    decl = {"numeric": "x(0)", "string": 'x("")', "bool": "x(false)"}.get(rec["returns"])
    body = f"Vars: {decl};\nx = {call};\n" if decl else f"{call};\n"
    return "Array: A[5](0);\n" + body


def run_status(name, source, bars, config):
    """Run ``source``; return its status text, or raise if the example is broken."""
    _rt_core.pl_unimplemented_reset()
    try:
        run_el(source, bars, config, max_bars_back=10)
    except _rt_core.UnimplementedBuiltinError:
        return STATUS_STOPS
    except Exception as exc:
        raise RuntimeError(f"reference example for {name} failed: {type(exc).__name__}: {exc}\n"
                           f"--- source ---\n{source}") from exc
    hit = _rt_core.pl_unimplemented_report()
    return STATUS_NOOP if any(k.lower() == "pl_" + name.lower() for k in hit) else STATUS_RUNS


def load_notes():
    with open(NOTES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def arity_text(rec):
    lo, hi = rec["min_arity"], rec["max_arity"]
    if rec["zero_arg_property"] and lo == 0:
        return "none (used as a value)"
    if hi is None:
        return f"at least {lo}" if lo else "0 or more"
    return str(lo) if lo == hi else f"{lo} to {hi}"


def render_entry(rec, note, status):
    name = rec["keyword"]
    lines = [f"### {name}", ""]
    level = LEVEL_TEXT[rec["verification"]]
    if rec["verification"] == "verified:test":
        level += f" (`{rec['verified_by']}`)"
    returns = rec["returns"] if rec["returns"] != "unknown" else "none / not established"
    lines += [
        "| Kind | Returns | Arguments | Argument types | Evidence | On MC compile list | Run status |",
        "|---|---|---|---|---|---|---|",
        f"| {rec['kind']} | {returns} | {arity_text(rec)} | "
        f"{', '.join(rec['arg_types']) if rec['arg_types'] else 'not established'} | "
        f"{level} | {'yes' if catalog.is_verified(name) else 'no'} | {status} |",
        "",
        note["desc"],
        "",
    ]
    if note.get("example"):
        lines += ["```easylanguage", note["example"], "```", ""]
    return lines


def build():
    """Return {file name: content} for every page in docs/reference/."""
    notes = load_notes()
    entries = notes["entries"]
    recs = selected()
    names = {r["keyword"] for r in recs}
    missing = sorted(names - set(entries), key=str.lower)
    extra = sorted(set(entries) - names, key=str.lower)
    if missing or extra:
        raise SystemExit(f"_notes.json does not match the catalog selection: "
                         f"missing {missing}, unexpected {extra}")
    topics = {t["slug"]: t for t in notes["topics"]}
    bad = sorted(n for n, e in entries.items() if e["topic"] not in topics)
    if bad:
        raise SystemExit(f"_notes.json entries with an unknown topic: {bad}")

    bars = load_bars_csv(BARS_PATH)[:N_BARS]
    config = get_config("NQ")
    by_topic = {slug: [] for slug in topics}
    status_counts = {}
    for rec in recs:
        note = entries[rec["keyword"]]
        source = note.get("example") or probe_source(rec)
        status = run_status(rec["keyword"], source, bars, config)
        status_counts[status] = status_counts.get(status, 0) + 1
        by_topic[note["topic"]].append((rec, note, status))

    pages = {}
    header = ("<!-- GENERATED by tools/gen_reference.py from the catalog and "
              "docs/reference/_notes.json; do not edit by hand. -->")
    for slug, topic in topics.items():
        lines = [header, "", f"# {topic['title']}", "", topic["intro"], "",
                 "[Back to the keyword reference index](README.md)", ""]
        for rec, note, status in by_topic[slug]:
            lines += render_entry(rec, note, status)
        pages[f"{slug}.md"] = "\n".join(lines).rstrip("\n") + "\n"

    idx = [header, "", "# EasyLanguage keyword reference", "",
           "A reference for the EasyLanguage / PowerLanguage keywords PowerBridge supports, "
           "written for PowerBridge from its own code, tests and runs. It covers "
           f"{len(recs)} builtins: every catalog builtin on the MultiCharts compile list, "
           "plus every builtin a PowerBridge test or capture-backed strategy uses. "
           "Query the full catalog with `python -m pl_transpiler.catalog`.", "",
           "## Reading an entry", "",
           "- **Returns**: the value type, or *none / not established* for statements such "
           "as orders and for words whose type PowerBridge has not pinned down.",
           "- **Arguments**: how many arguments the keyword takes. *none (used as a value)* "
           "means it is written without parentheses.",
           "- **Evidence**: *PowerBridge test* means EL in the named test calls the keyword. "
           "*capture (strategy-level)* means a strategy that uses it matches a private "
           "MultiCharts capture bar for bar; the keyword's own value is not checked in "
           "isolation, and the check skips in a public clone. *unverified* means "
           "PowerBridge has no test evidence yet.",
           "- **On MC compile list**: the name is in `mc_verified_builtins.txt`, the list "
           "of builtins known to compile in MultiCharts. It says nothing about "
           "PowerBridge's behaviour.",
           "- **Run status**: what happened when the reference build ran the example (or a "
           "minimal call) through `run_el`: *runs*; *accepted; no effect in a backtest*; or "
           "*transpiles; the run stops* when the runtime does not implement it.",
           "",
           "Examples are short fragments. Each one is transpiled and run on sample bars by "
           "`tests/test_reference_docs.py`.", "",
           "## Topics", "",
           "| Topic | Entries |", "|---|---|"]
    for slug, topic in topics.items():
        idx.append(f"| [{topic['title']}]({slug}.md) | {len(by_topic[slug])} |")
    idx += ["", "## Run status totals", "", "| Status | Entries |", "|---|---|"]
    for status in (STATUS_RUNS, STATUS_NOOP, STATUS_STOPS):
        idx.append(f"| {status} | {status_counts.get(status, 0)} |")
    idx += ["", "## All keywords", ""]
    for rec in recs:
        slug = entries[rec["keyword"]]["topic"]
        idx.append(f"- [{rec['keyword']}]({slug}.md#{rec['keyword'].lower()})")
    pages["README.md"] = "\n".join(idx) + "\n"
    return pages


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pages = build()
    if argv == ["--check"]:
        stale = []
        for fname, text in sorted(pages.items()):
            path = os.path.join(REF_DIR, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    same = fh.read() == text
            except FileNotFoundError:
                same = False
            if not same:
                stale.append(fname)
        on_disk = {f for f in os.listdir(REF_DIR) if f.endswith(".md")}
        stale += sorted(on_disk - set(pages))
        if stale:
            print(f"docs/reference is stale ({', '.join(stale)}): run {REGEN_CMD}")
            return 1
        print("docs/reference is up to date")
        return 0
    for fname, text in pages.items():
        with open(os.path.join(REF_DIR, fname), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    print(f"wrote {len(pages)} pages to {REF_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
