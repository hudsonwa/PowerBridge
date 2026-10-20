<!--
 Copyright 2026 Joshua Hudson

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
-->


# The optional `accel` fast path (decision record)

> **Status note (0.2.0):** this file is the design record from the phase-1
> rollout. The native kernel has since shipped strategy execution
> (`run_program`, Data2, sweeps, trace). For current behaviour, read
> `README.md` and `AGENTS.md` (`pl_transpiler.runtime.accel`). Issue numbers
> refer to the original private tracker.

This is the recorded decision behind issue [#17](https://github.com/hudsonwa/PowerBridge/issues/17):
the product shape of the optional native fast path, written **before** any kernel
code exists, so the boundary is agreed rather than discovered later. It changes no
behaviour. It promises no speed. This document is the contract; the kernel, CI, and
conformance tickets implement against it.

## The interface (frozen — shared across the kernel, CI, and test lanes)

These three facts are the whole public interface of the fast path. Do not rename
them anywhere:

| Fact | Value |
|---|---|
| Extra name | `accel` — `pip install .[accel]` (source tree) / `pl-transpiler[accel]` (distribution name) |
| Native extension import path | `pl_transpiler._accel` |
| Extra absent | `import pl_transpiler._accel` raises `ImportError`; callers **fall back to the Python path loudly** and produce identical results |

Because these three facts are shared verbatim with the kernel lane (#18), the CI/wheel
lane (#21), and the test lane, they are frozen here: renaming any of them in a later
PR is a contract break, not a cleanup.

The fallback rule in the third row is not decoration — it is the load-bearing
invariant, restated in the Invariants section of [AGENTS.md](../AGENTS.md) and in
[SECURITY.md](../SECURITY.md): **any construct the kernel cannot faithfully execute
must raise or fall back to Python loudly — it must never silently return a different
result.**

## What is *not* changing

- `pyproject.toml` `dependencies` stays `[]` — that is the product rule stated at
  the top of the file ("Pure-stdlib: the transpiler, emitter, runtime and gates
  import nothing off PyPI"). The transpiler, emitter, runtime, and gates import
  nothing off PyPI on the default path. `pip install .`
  remains a pure-standard-library, zero-dependency, no-build-step install, and that
  is the product.
- Without the extra, `pl_transpiler` behaves **bit-identically to today**: same
  Python path, same trades, same digests, same test tier. The extra is strictly
  additive.
- The default CI job never requires a Rust toolchain. Contributors without one keep
  running the whole public tier exactly as before.

## How installing the extra actually yields `pl_transpiler._accel` — the honest mechanism

Documenting the extra is the easy half. The hard half is *how `pip install .[accel]`
results in a usable native module at all*. Two mechanisms that look obvious are
ruled out by this repository's structure, and the conclusion is a product-level
decision this document deliberately does **not** make:

- **A build requirement cannot work.** The extra's dependencies are installed and
  *built by pip before* the project itself is built. A native kernel is the very
  thing the installed `pl_transpiler` must import at *run* time, so any build
  backend that tries to compile the kernel's output into *this* wheel at build
  time either deadlocks against pip's install ordering or lands the binary in
  the wrong artifact — the kernel's wheel. (A build-time-only toolchain in
  `[build-system] requires` — plain `cargo`, no `pl_transpiler` import — is how
  the single-distribution shape does compile, but see alternative 2 for why that
  shape is not chosen today.)
- **A runtime dependency on a pre-built kernel wheel is the mechanism — with a
  consequence that must be stated.** The dependency installs cleanly (it does
  not need `pl_transpiler` to import), and unpacking its wheel drops
  `pl_transpiler/_accel.*.so` into the installed `pl_transpiler` directory,
  where the core finds it as `pl_transpiler._accel`. What it means: the core
  and the kernel become **two distributions that jointly own one package
  namespace**, a familiar packaging pattern for optional native accelerators,
  and the convention it requires is that the kernel distribution's wheel metadata
  declares the shared files so the two installs do not fight over ownership of
  the same paths (and the kernel wheel must not restack files belonging to the
  core distribution).

So the real mechanism is this: **`pip install .[accel]` works only by declaring
one dependency that itself ships the native extension module
`pl_transpiler._accel`** — a second, separately built distribution whose wheel
installs that module into the `pl_transpiler` package directory. As of this
writing **that distribution does not exist**: it is owned by ticket #18 (the
kernel), #21 (the wheel builds), and a publishing decision this document
deliberately does **not** make.

The rejected alternatives, and why — each is stated so nobody quietly ships a
broken install or a fake fast path:

1. **Point the extra at a git checkout as a dependency**
   (`accel = ["pl-transpiler-accel @ git+https://github.com/…"]`): pip's dependency
   resolver rejects direct VCS URLs in dependency specifiers — that syntax is only
   valid for requirements files and even there it is not installable from an index.
   This *cannot* be made to work in `pyproject.toml`; do not attempt it.
2. **Ship the Rust extension inside this same package via a custom
   `[build-system]` backend** (a maturin/PyO3-style backend that replaces the
   setuptools backend and shells out to `cargo`):
   it would make `pip install .[accel]` literally true on the source tree, but it
   couples every consumer's install to a Rust toolchain at build time and breaks
   the "core has no build step" promise for anyone whose wheel is not cached. It is
   a legitimate future shape, not today's decision.
3. **Make the extra a no-op or a source-tree-only marker** (declare it, depend on
   nothing): installing it would change nothing, and users would reasonably read
   `[accel]` as "the fast path is active" when it is not. This is the *silent wrong
   result* this project exists to refuse, in the install script itself. Rejected.

Because none of those is both true and achievable inside this repository alone, the
decision recorded here is the fourth option: **a second distribution, published
separately, whose wheel provides `pl_transpiler._accel`.** Which artifact — name,
index (PyPI-free, a private index, GitHub releases, or nothing yet), platform matrix,
and who publishes it — is **an open product-level decision, flagged on issue #17**,
not invented here. Until that decision lands and the artifact exists,
`pip install .[accel]` is documented as *declared but not yet satisfiable*: it is
reserved to make the future install honest, and the extra name `accel` is frozen so
the kernel lane (#18), the CI/wheel lane (#21), the conformance lane (#20), and the
test lane can build against one shared name instead of four guesses.

What is true *today*, verifiably, without the artifact:

```bash
pip install .            # works, zero dependencies, zero build step
python3 -c "import pl_transpiler._accel"   # -> ModuleNotFoundError
```

(`ModuleNotFoundError` — CPython's "no module named …" error from the default
importer — is a subclass of `ImportError` (verified on the 3.9 floor this project
supports), so the frozen contract's `ImportError` is raised by every compliant
importer and caught by every compliant caller.)

That `ImportError` **is** the extra-absent behaviour from the frozen contract, and
it is correct: callers must treat it as "run the Python path, and say so", never as
a failure to swallow.

## Trust model (summary; the normative text is SECURITY.md)

Today [SECURITY.md](../SECURITY.md) and [AGENTS.md](../AGENTS.md) tell you that
`pl_run` and `pl_oracle` **execute generated Python** on your machine. The accel
path is a **different trust surface**: a compiled kernel you cannot read, a binary
whose arithmetic is not reviewable in the way Python source is reviewable. Installing
the extra is trusting the wheel's publisher and its toolchain **in addition to**
trusting the strategy file. If you cannot audit that, you should not accelerate.

The equivalence bar: an `accel` run must produce the **exact same trade list** —
same fills, same order, same count — as the Python path (the oracle digest of #11),
not "close". A fast path that disagrees is a bug, not a feature, and the fail-loud
rule above is what turns disagreement into an error instead of a silent wrong answer.

## How to tell which path ran

No feature is a silent feature. Until the kernel lands there is exactly one path,
and it is the Python path; the commands say so in their output:

```bash
python3 -c "import pl_transpiler._accel" 2>&1 | tail -1
# ModuleNotFoundError: No module named 'pl_transpiler._accel' -> Python path.
```

When the kernel lane lands, the run entry points (`pl_run`, `pl_oracle`) will print
a one-line banner on stderr stating the path used — for example
`[pl_run] path=accel (pl_transpiler._accel <version>)` or
`[pl_run] path=python (accel extra not installed)` — and the kernel must provide a
compatibility marker checked at import time so a wheel built for a different dialect
version raises loudly instead of running stale semantics (see #21). A missing or
broken accel must degrade to the Python path with a clear message — never crash,
never silently produce a different result.

## Verification tiers, accel

Mirroring the test-tier map in [AGENTS.md](../AGENTS.md): the public tier
(`python3 tests/run_public_tier.py`) exercises the Python path only and must stay
green with or without the extra — it never requires Rust. The differential
conformance harness (#20) and the full-tape fidelity gate (#19) are the gates that
keep the fast path honest; until they exist, the accel path, if built, must not be
trusted to run. Do not measure performance against the accel path while driving in
conditions it has never been proven to match; this document makes **no**
performance claims.

## Wheels and CI (issue #21)

The `accel` crate (`accel/` at the repo root) is a **separate distribution** whose
wheel installs `pl_transpiler._accel` next to the core package. The root
`pyproject.toml` `dependencies` list stays `[]`; the `accel` extra remains the
reserved binding point and is not required by anything.

- **Build (verified on a fresh arm64 macOS box, Apple M5 Max, Rust 1.98.1 /
  maturin 1.15.0):**

  ```bash
  cd accel
  maturin build --release
  # -> accel/target/wheels/accel-0.2.0-cp39-cp39-macosx_11_0_arm64.whl
  pip install ../target/wheels/accel-*.whl   # or pip install accel/target/wheels/*.whl from the root
  ```

  The wheel contains `pl_transpiler/_accel*.so` (the mixed-layout `python-source`
  in `accel/pyproject.toml` makes maturin place the cdylib at that path). Import
  from a cwd that is **not** the repo root, or the checkout's source `pl_transpiler`
  shadows the installed module (see `accel/README.md`).

- **What CI runs** (`.github/workflows/accel.yml`, issue #21): two jobs.
  `accel-macos-arm64` **runs the accel path**, not merely builds it — it installs
  the extra, imports `pl_transpiler._accel` from a non-repo cwd, asserts
  `ping() == 42` and `run()` raising `NotImplementedError`, then runs the public
  test tier and the differential conformance harness. That job **fails if the
  extra silently does not load**. `core-no-accel` installs core without the extra
  and proves the zero-dependency promise is itself tested: the import raises
  `ImportError`, and the public tier + conformance harness stay green.

- **Platform matrix.** macOS arm64 is the platform actually built and verified
  (this lane's M5 Max evidence and the `accel-macos-arm64` CI job). Linux
  x86_64/aarch64 and Windows wheels are **clearly-marked follow-ups** — nothing
  in this lane claims a platform that was not built. Do not add an unbuilt
  platform to CI as if it worked.

- **Phase-1 boundary.** The shipped crate exposes `ping()` (returns 42) and a
  `run()` that raises `NotImplementedError` with the frozen bars-in/trades-out
  signature. That is all — no kernel semantics, no fills, no performance claim.
  The differential conformance harness's python-vs-accel gate arms in phase 2 once
  the kernel's execution boundary is frozen; in phase 1 it skips cleanly from the
  repo root, and the wheel-load gate is the phase-1 proof that the extra loaded.
