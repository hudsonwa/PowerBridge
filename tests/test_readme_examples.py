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

"""Run the README and usage-guide examples and compare the output they document.

Every ```bash / ```python block in README.md, then docs/USAGE.md, runs, in order, in one scratch
directory that holds copies of examples/ and ground_truth/ (so the README's
"run from the repository root" paths resolve). Console commands
(pl_transpile, pl_run, pl_oracle, el_emit) run as `python -m` modules so the
test does not need an installed distribution.

Markers (HTML comments, invisible on GitHub), on the line(s) before a block:

  <!-- readme-test: skip (reason) -->   do not run the next block
  <!-- readme-test: file NAME -->       the next plain block is NAME's content

A plain ``` block directly after a runnable block (blank lines only between)
is that block's documented output. Documented lines must appear in the actual
output in order; a `...` line stands for any lines. `note:` lines are about
whether the optional native module is installed and are ignored.

`out/` is removed at the start of every `## ` section, so each section must
create the directories it writes to, as a reader who jumps straight to it would.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO, "README.md")
DOCS = [README, os.path.join(REPO, "docs", "USAGE.md")]
COMMANDS = {
    "pl_transpile": "pl_transpiler.tools.pl_transpile",
    "pl_run": "pl_transpiler.tools.pl_run",
    "pl_oracle": "pl_transpiler.tools.pl_oracle",
    "el_emit": "pl_transpiler.tools.el_emit",
}
MARKER = re.compile(r"<!--\s*readme-test:\s*(skip|file)\b\s*(.*?)\s*-->")
FENCE = re.compile(r"^```(\w*)\s*$")


def parse_readme(text):
    """Return [{lang, body, line, skip, output, file}] for every fenced block."""
    lines = text.splitlines()
    blocks, pending, i = [], {}, 0
    while i < len(lines):
        m = MARKER.search(lines[i])
        if m:
            pending[m.group(1)] = m.group(2) or True
            i += 1
            continue
        f = FENCE.match(lines[i])
        if not f:
            if lines[i].strip():
                pending = {}
            i += 1
            continue
        start = i
        i += 1
        body = []
        while not lines[i].startswith("```"):
            body.append(lines[i])
            i += 1
        i += 1
        section = next((l for l in reversed(lines[:start]) if l.startswith("## ")), "")
        blocks.append({"lang": f.group(1), "body": body, "line": start + 1,
                       "section": section,
                       "skip": pending.get("skip"), "file": pending.get("file"),
                       "output": None})
        pending = {}
        # remember whether only blank lines separate this block from the next
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        blocks[-1]["next_adjacent"] = j < len(lines) and FENCE.match(lines[j]) is not None
    runnable = []
    for k, b in enumerate(blocks):
        if b["lang"] in ("bash", "python"):
            if k + 1 < len(blocks) and b["next_adjacent"] and blocks[k + 1]["lang"] == "":
                b["output"] = blocks[k + 1]["body"]
            runnable.append(b)
        elif b["lang"] == "" and b["file"]:
            runnable.append(b)
    return runnable


def shell_commands(body):
    """Join `\\` continuations and drop comments; one argv per command."""
    cmds, cur = [], ""
    for line in body:
        line = line.rstrip()
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        argv = shlex.split(cur, comments=True)
        if argv:
            cmds.append(argv)
        cur = ""
    return cmds


def to_argv(argv):
    if argv[0] in COMMANDS:
        return [sys.executable, "-m", COMMANDS[argv[0]]] + argv[1:]
    if argv[0] in ("python", "python3"):
        return [sys.executable] + argv[1:]
    if argv[:2] == ["mkdir", "-p"]:
        return [sys.executable, "-c",
                "import os, sys; [os.makedirs(p, exist_ok=True) for p in sys.argv[1:]]"] + argv[2:]
    raise AssertionError(
        f"README command {argv[0]!r} is not runnable here; mark the block "
        "<!-- readme-test: skip (reason) -->")


def matches(doc, actual):
    """Documented lines appear in order in actual; '...' is a gap."""
    doc = [l.rstrip() for l in doc if not l.startswith("note:")]
    actual = [l.rstrip() for l in actual if not l.startswith("note:")]
    pos = 0
    for want in doc:
        if want == "...":
            continue
        while pos < len(actual) and actual[pos] != want:
            pos += 1
        if pos == len(actual):
            return False
        pos += 1
    return True


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("readme")
    shutil.copytree(os.path.join(REPO, "examples"), d / "examples")
    os.mkdir(d / "ground_truth")
    for name in os.listdir(os.path.join(REPO, "ground_truth")):
        if name.endswith(".txt"):
            shutil.copy(os.path.join(REPO, "ground_truth", name), d / "ground_truth")
    return d


def run(argv, cwd):
    env = dict(os.environ, PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=50)
    return p.returncode, p.stdout.splitlines(), p.stderr.splitlines()


def doc_blocks():
    """(file label, block) for every runnable block, README first."""
    for path in DOCS:
        for b in parse_readme(open(path, encoding="utf-8").read()):
            yield os.path.relpath(path, REPO).replace(os.sep, "/"), b


def test_readme_parses_runnable_blocks():
    blocks = [b for _, b in doc_blocks()]
    assert sum(1 for b in blocks if not b["skip"]) >= 8
    assert sum(1 for b in blocks if b["output"] and not b["skip"]) >= 5


def test_readme_examples(workdir):
    failures, section = [], None
    for doc, b in doc_blocks():
        if (doc, b["section"]) != section:
            section = (doc, b["section"])
            shutil.rmtree(workdir / "out", ignore_errors=True)
        if b["skip"]:
            continue
        where = f"{doc}:{b['line']}"
        if b["lang"] == "":
            path = workdir / b["file"]
            if not path.exists() or not matches(b["body"], path.read_text().splitlines()):
                failures.append(f"{where}: {b['file']} does not match the README")
            continue
        if b["lang"] == "python":
            script = workdir / "_readme_block.py"
            script.write_text("\n".join(b["body"]) + "\n")
            results = [run([sys.executable, str(script)], workdir)]
        else:
            results = [run(to_argv(a), workdir) for a in shell_commands(b["body"])]
        for rc, out, err in results:
            if rc != 0:
                failures.append(f"{where}: exit {rc}\n" + "\n".join(err[-10:]))
        if b["output"] is not None and not any(
                matches(b["output"], out + err) or matches(b["output"], err + out)
                for _, out, err in results):
            got = "\n".join(l for _, o, e in results for l in e + o)
            failures.append(f"{where}: output differs from the README\n--- README\n"
                            + "\n".join(b["output"]) + "\n--- actual\n" + got)
    assert not failures, "\n\n".join(failures)


def test_no_dead_relative_links():
    dead = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "target", ".venv", "node_modules")]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            # Fenced code is not markdown: EL such as `A[4](0)` there is not a link.
            text = re.sub(r"(?ms)^```.*?^```", "", open(path, encoding="utf-8").read())
            for m in re.finditer(r"\]\(([^)\s]+)\)", text):
                target = m.group(1).split("#")[0]
                if target and not re.match(r"[a-z]+:", target) and \
                        not os.path.exists(os.path.join(root, target)):
                    dead.append(f"{os.path.relpath(path, REPO)}: {m.group(1)}")
    assert not dead, "\n".join(dead)
