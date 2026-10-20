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


# AGENTS.md addendum — the optional `accel` fast path

> **Status note (0.2.0):** this file is the design record from the phase-1
> rollout. The native kernel has since shipped strategy execution
> (`run_program`, Data2, sweeps, trace). For current behaviour, read
> `README.md` and `AGENTS.md` (`pl_transpiler.runtime.accel`). Issue numbers
> refer to the original private tracker.

Companion to [docs/ACCEL.md](../ACCEL.md) (the decision record) and
[SECURITY.md](../../SECURITY.md) (the normative trust text). This file is normally the
machine-facing contract; this addendum states, in the same voice, the facts an agent
must honor around the optional fast path.

> **Editing note for the maintainer.** The three facts below were to be
> woven into the run_el paragraph of "Python API contracts", the "Invariants" list,
> and the "Limitations (contract form)" list of this file. An approval gate on
> protected agent-instruction files timed out during that change and the fold could
> not be applied, so the text ships here, complete, awaiting merge.

## The interface (frozen — shared across the kernel, CI, and test code)

- The optional extra is named **`accel`** (`pip install .[accel]`; distribution
  form `powerbridge[accel]`). Do not rename it.
- The native extension's import path is **`pl_transpiler._accel`**. Do not rename it.
- **Extra absent** → `import pl_transpiler._accel` raises `ImportError`; callers
  **fall back to the Python path loudly** and results are **identical** — same
  Python path, same trades, same oracle digests, bit-for-bit today's behaviour.
  The extra is opt-in; the default install stays pure standard library with
  `dependencies = []` (the product rule at the top of `pyproject.toml`).
- **Extra present** → the kernel may run, and its trade list must equal the Python
  path's **exactly** — same fills, same order, same count (the `pl_oracle` digest). "Close" is a divergence and a defect.
- No build requirement and no `[build-system]` entry of this distribution may ever
  pull the native kernel into the **core** install path. If the accel path cannot
  be produced by a second distribution plus one extra, that is a product decision
  to flag to the maintainer — see docs/ACCEL.md for the mechanism analysis, the
  rejected variants, and the open publishing decision. It is not for an agent to
  resolve silently.
- **The accel wheel is a separate distribution and the extra stays optional.** The `accel/` crate builds a wheel (`maturin build --release`;
  macOS arm64 is built and verified, other platforms are follow-ups). CI in
  `.github/workflows/accel.yml` RUNS the accel path — installs the wheel, imports
  `pl_transpiler._accel` from a non-repo cwd, asserts `ping()==42` and `run()`
  raising `NotImplementedError`, then runs the public tier and the differential
  conformance harness — and a second job proves the zero-dep promise by running
  the same tier with the extra absent (import must raise `ImportError`). The
  wheel exposes only the phase-1 `ping()`/`run()` boundary; no kernel semantics,
  no performance claim.

## TRUST MODEL addendum (extends every `TRUST MODEL` paragraph above)

`run_el`, `pl_run`, and `pl_oracle` **execute generated Python** on your machine;
only run strategy files you trust (converting/transpiling alone never executes the
input). The optional accel path adds a second, different trust surface: a native
kernel `pl_transpiler._accel` is a compiled binary you **cannot read** the way you
read emitted Python. Installing the extra means trusting the wheel's publisher and
its build toolchain **in addition to** trusting the strategy file. If you cannot
audit the wheel's provenance, do not accelerate — the default `pip install .`
remains the auditable, dependency-free path, and the absent extra is the normal,
documented state, not a failure. See [SECURITY.md](../../SECURITY.md).

## Fail-loud rule, restated for the accel path

The existing invariant — **"Fail-loud everywhere … no component emits or runs
plausible-but-wrong output"** — governs the kernel exactly as it governs the
transpiler, `emit_el`, `py_front`, and the runtime:

> **Any construct the kernel cannot faithfully execute must raise or fall back to
> the Python path loudly. It must never silently return a different result.**

Loudly means: an exception, or a clear stderr notice that the Python path ran —
never a silent switch, never a silent approximation, never a result that merely
looks right. An `accel` run whose trade list disagrees with the Python path is a
bug to report through SECURITY.md, never a feature to rely on. Until the
conformance harness and the fidelity gate exist, the accel path — if
built at all — is not to be trusted to run.

## How to tell which path ran

`import pl_transpiler._accel` is the existence check:

```bash
python3 -c "import pl_transpiler._accel" 2>&1 | tail -1
# ModuleNotFoundError: No module named 'pl_transpiler._accel'  -> Python path
```

`ModuleNotFoundError` (CPython's default-importer "no module named …" error) is a
subclass of `ImportError` — verified on the 3.9 floor this package supports — so
catching `ImportError` per the contract catches every compliant importer's
"extra absent" signal. When the kernel work lands, the run entry points
(`pl_run`, `pl_oracle`) print a one-line banner naming the path used — e.g.
`[pl_run] path=accel (…)` vs `[pl_run] path=python (accel extra not installed)` —
and the kernel exposes a compatibility marker checked at import time, so a wheel
built against a different dialect raises rather than running stale semantics. Until then there is exactly one path, and it is the Python path.

## Limitations addendum (contract form)

- The `[accel]` extra is **declared but empty**, and that is on purpose
  (docs/ACCEL.md): it is reserved as the future binding point for the second
  distribution, not a no-op marker users may read as "the fast path is active".
- No performance figures apply to the accel path. Any number seen in a future
  change is to be taken from the full-tape measurement protocol, never from an
  estimate, and never before the conformance gate is green.
