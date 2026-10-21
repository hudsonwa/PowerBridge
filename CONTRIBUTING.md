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
CI uses it on every OS).

Please do not add dependencies. The library is standard-library only.

By contributing you agree your work is licensed under Apache 2.0,
same as the rest of the project.
