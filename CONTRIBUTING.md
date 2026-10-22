# Contributing

Thank you for looking at PowerBridge.

## Propose something first

Open an issue before a large PR. A short note is enough:
what you want to change, and why.

Small, obvious fixes (typos, a failing public test) can be a PR
without an issue.

## How to send a change

1. Fork the repo.
2. Create a branch.
3. Keep the change small and readable.
4. Run the tests: `python3 -m pytest -q --timeout=60`
5. Open a pull request and say what you changed.

## Running checks locally

CI runs these on every pull request. The tools are for development only;
the library itself still needs nothing beyond the standard library.

```sh
python3 -m pip install pytest pytest-timeout ruff
python3 -m pytest -q --timeout=60  # everything, including the public tier
ruff check .                       # lint (rules in pyproject.toml)
```

`pytest -m public_tier` runs only the portable public tier. The same tier
also runs without pytest as `python3 tests/run_public_tier.py` (stdlib only;
CI uses it on every OS). Capture-backed gates skip cleanly when the large
MultiCharts CSVs are absent; see [docs/CAPTURES.md](docs/CAPTURES.md) if you
want to attach your own.

On a fresh clone over a hundred tests skip; that is expected. They fall into
three groups, each skipped with a one-line reason:

- **Native kernel** (the large majority): parity, batch and gate tests for the
  optional Rust module (`pl_transpiler._accel`). Build it with
  `tools/build_accel.sh` (needs a Rust toolchain; see [docs/ACCEL.md](docs/ACCEL.md))
  and they run, except a handful kept for programs the kernel refuses (arrays,
  some functions), which fall back to Python. CI runs them in the
  `accel (macOS arm64)` job of `.github/workflows/accel.yml`, which builds the
  wheel and runs the full pytest suite with the kernel loaded.
- **Licensed data**: drawing-corpus, capture and audit-tape tests need
  MultiCharts data, which is not redistributed (see [docs/CAPTURES.md](docs/CAPTURES.md)).
  No CI job runs them; they run only where that data is attached locally.
- **Opt-in checks**: a wall-clock check (`PB_PERF=1`), the wheel-build test
  (needs `build` and setuptools>=77) and a few known round-trip gaps. CI does
  not set `PB_PERF`.

Every pytest run ends with a skip summary that groups skips by reason with
counts; `python3 -m pytest -q -rs --timeout=60` lists each skipped test.

## Keyword reference and catalog: where facts come from

`docs/reference/` and the keyword catalog (`pl_signatures.jsonl`) describe
EasyLanguage as PowerBridge supports it. Every fact in them must come from
PowerBridge itself:

- its code, its tests, or behaviour you see by running it;
- a parity test or capture result that is already in the repo.

Never copy or paraphrase vendor documentation (MultiCharts or TradeStation
manuals, help files, PDFs), and don't have it open while you write an entry.
Write descriptions and examples in your own words. If you can't show a
behaviour with PowerBridge, mark it `documented-unverified` rather than
stating it as fact. After editing `docs/reference/_notes.json`, run
`python3 tools/gen_reference.py` and commit the regenerated pages.

## Dependencies

Please do not add dependencies. The library is standard-library only.

By contributing you agree your work is licensed under Apache 2.0,
same as the rest of the project.
