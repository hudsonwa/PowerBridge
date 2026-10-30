# powerbridge-accel — native kernel for PowerBridge

`powerbridge-accel` is the optional native (Rust) kernel for
[PowerBridge](https://github.com/hudsonwa/PowerBridge), the
EasyLanguage / PowerLanguage strategy backtester. It runs supported strategies
about 5x faster than PowerBridge's Python engine and gives the same trades:
any difference from the Python path is a bug.

You do not need it. Without it PowerBridge runs everything on the Python path
with identical results.

## Install

Install it next to `powerbridge`, with the same version:

```bash
pip install powerbridge-accel==0.13.0
```

Prebuilt wheels cover macOS (arm64, x86_64), Linux (x86_64, aarch64) and
Windows (x64), Python 3.9 and later. The same wheels, with `SHA256SUMS`, are
attached to each [GitHub Release](https://github.com/hudsonwa/PowerBridge/releases).

Check that PowerBridge sees it:

```bash
pl_oracle --version
# pl_oracle 0.13.0
# native kernel: powerbridge-accel 0.13.0 (available)
```

## What it runs

The kernel runs a strategy only when it can do so exactly as the Python path
does. Anything it cannot run faithfully is refused before the run, and
PowerBridge runs the Python path instead and says so on stderr. It never
gives a silently different answer. `pl_oracle --explain-native FILE` lists
every construct in a strategy that the kernel would refuse.

Coverage, speed figures and the full list of refused constructs are in
[docs/ACCEL.md](https://github.com/hudsonwa/PowerBridge/blob/main/docs/ACCEL.md).

## Building from source

The crate lives in `accel/` in the PowerBridge repository and builds the
extension module `pl_transpiler._accel` (the module and the `accel` extra
names are stable). From a clone, in a virtual environment with Rust and
maturin on `PATH`:

```bash
cd accel
maturin develop --release
cd ..   # or: tools/build_accel.sh, which installs it into the source tree
python -c "import pl_transpiler._accel as a; print(a.ping())"   # 42
```

Import it from a directory other than the repository root: there, the
`pl_transpiler` source package shadows the installed one, and the extension
is only found if it was copied into the source tree (`tools/build_accel.sh`
does that). On macOS a plain `cargo build` needs the `dynamic_lookup`
rustflags in `.cargo/config.toml`; maturin passes them itself.

`cargo test --release` runs the Rust unit and parity tests; the
native-vs-Python tests are the `needs_native` pytest tests (see
[CONTRIBUTING.md](https://github.com/hudsonwa/PowerBridge/blob/main/CONTRIBUTING.md)).

## License

Apache-2.0, like PowerBridge.
