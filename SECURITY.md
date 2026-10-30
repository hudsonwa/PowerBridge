# Security

If you think you have found a security issue, please do not open a public
GitHub issue.

Report it privately through GitHub's private vulnerability reporting:
[open a draft security advisory](https://github.com/hudsonwa/PowerBridge/security/advisories/new).
Only the maintainer can see it, and we will reply there.

## What you are trusting when you run PowerBridge

`pl_run` executes generated Python on your machine. Only run strategy files
you trust. Transpiling a file does not execute it.

## The optional `accel` fast path is a different trust surface

Today everything above refers to executing **generated Python** — source you
can read before you run it. The optional `accel` extra (the
decision record is [docs/ACCEL.md](docs/ACCEL.md)) introduces a native
kernel, the extension module `pl_transpiler._accel`. A compiled binary is not
reviewable the way Python source is: you cannot read its arithmetic, its fill
timing, or its rounding, and you cannot diff it against the Python path. That
is a genuinely different trust decision, and installing the extra is you
choosing to make it.

When you install the `powerbridge-accel` wheel you are trusting, on top of the trust
you already place in a strategy file:

- **the publisher of the native wheel** — whoever produced the binary the
  installer downloads, and the signing or provenance signals you accept at
  install time;
- **the build toolchain** behind that binary (a Rust/cargo + PyO3 build via
  maturin), and everything it pulls in;
- **a binary blob that can change the trade list without being readable.**
  The Python path gives you an audit trail; the kernel path does not.

The fail-loud rule, restated for the accel path (the same principle the rest
of the repo enforces — "unsupported constructs raise; they are never
guessed"):

> **Any construct the kernel cannot faithfully execute must raise or fall back
> to the Python path loudly. It must never silently return a different
> result.** An `accel` run that disagrees with the Python path on the trade
> list is a defect to be reported, and the trade-list oracle
> (`pl_oracle`, docs/ACCEL.md) is how you prove the two paths agree.

And the mirror rule on the install side: **when the extra is absent, nothing
changes.** `import pl_transpiler._accel` raises `ImportError`, callers fall
back to the Python path *and say so*, and results are identical — same trades,
same digests, bit-for-bit to the default install. An absent or broken `accel`
module is a normal, documented state; it must never crash a run and must never
be confused for the fast path having run.

### Verify a downloaded wheel

Releases after v0.11.0 carry a `SHA256SUMS` file covering every sdist and
wheel, and a GitHub build provenance attestation for each of those files.
Download the file you want and `SHA256SUMS` from the same release into one
folder, then run:

<!-- readme-test: skip (needs downloaded release files and the gh CLI) -->
```bash
sha256sum -c SHA256SUMS --ignore-missing   # macOS: shasum -a 256 -c SHA256SUMS --ignore-missing
gh attestation verify <file> --repo hudsonwa/PowerBridge
```

The first checks the file was not changed after the release was built; the
second checks it was built by this repository's `release.yml` workflow.
v0.11.0 and earlier have neither.

If you cannot audit the native wheel's provenance, do not accelerate: the
default `pip install .` remains a pure-standard-library, zero-dependency,
auditable install forever — that is the product.
