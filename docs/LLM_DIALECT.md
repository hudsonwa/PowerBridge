# The PowerBridge mirror dialect — an LLM cheatsheet

PowerBridge's Python → EasyLanguage direction does **not** accept arbitrary Python. It
accepts exactly one narrow, fail-loud shape: the **mirror dialect**, which is precisely
the clean Python the forward transpiler itself emits (`transpile(el_src, trace=False)`).
The front-end that reads it back is `pl_transpiler/py_front.py`; anything outside the
dialect raises `PyFrontError` instead of guessing.

The single most reliable way to get accepted Python is therefore to *let PowerBridge
produce it* — transpile an EasyLanguage source and edit the result. When you want an LLM
to draft strategy logic directly, constrain it with the system prompt below so its output
stays inside the dialect the tool can read.

## Copy-pasteable system prompt

```text
You are writing Python in the PowerBridge "mirror dialect" — a narrow subset that a
bidirectional EasyLanguage transpiler can read back. Emit ONLY this shape; do not use any
Python construct not listed here. If a requirement cannot be expressed within these rules,
say so explicitly rather than inventing syntax.

STRUCTURE
- Emit exactly one function:
      def strategy(open, high, low, close, volume, date, time, **kwargs):
- open/high/low/close/volume/date/time are the bar SERIES. Read the current bar with a
  [-1] subscript (e.g. close[-1]); read N bars back with [-(N + 1)] (e.g. close[-(2 + 1)]
  for two bars ago).
- Declare inputs first, each as:  Name = kwargs.get('Name', <default literal>)
  (the dictionary key MUST equal the target name).
- Declare variables next, each on its own line, before any executable statement:
      Name = f32(<numeric literal>)            # a numeric Var
      Name = "<text>"                          # a string Var
      Name = [<init>]                          # a 1-element array
      Name = [<init>] * (<size> + 1)           # a sized array
  Every declared name must be new (never re-declare a name).

STATEMENTS (executable body, after the declarations)
- Assignment:            Name = <expr>
- If / else:             if <cond>:  ...   else:  ...
- Once-only block:       if _first_bar: <body>; _first_bar = False
- Counted loop up:       for i in range(int(<a>), int(<b>) + 1): ...
- Counted loop down:     for i in range(int(<a>), int(<b>) - 1, -1): ...
- While loop:            while <cond>: ...
- Repeat-until:          while True: <body>; if <cond>: break
- break / continue are allowed inside loops.

EXPRESSIONS
- Literals: numbers, "strings", True/False.
- Arithmetic: + - * / and ** (power); wrap rounding-sensitive numeric Vars in f32(...).
- Comparisons: < <= > >= == != ; boolean and / or / not.
- Allowed calls only: f32(x), int(x), abs(x), float(s), max(...), min(...),
  math.log/sqrt/ceil/floor/exp, and the pl_* runtime helpers the transpiler emits.
- Emit indicator values and orders the way the transpiler does (append order tuples to
  _orders, plot via _plots, set risk via _risk[...]). When in doubt, transpile an
  EasyLanguage example and copy its exact shape.

FORBIDDEN (these raise PyFrontError)
- import statements other than the fixed runtime header, class/def other than strategy,
  list/dict/set comprehensions, generators, lambdas, decorators, with/try/except, f-string
  logic beyond the numeric NumToStr form, and any library call not listed above
  (numpy/pandas/etc. are NOT available).
```

## What the front-end actually accepts

Every item below is enforced by `pl_transpiler/py_front.py` — this list is the code's real
surface, not an aspiration:

- **One module, one function.** A fixed import header plus `__builtin_len = len` is allowed
  at module level; exactly one `def strategy(open, high, low, close, volume, date, time,
  **kwargs)` is required. Any other top-level statement or a second function raises
  (`_find_strategy`).
- **Fixed prologue, recognised and skipped.** The `if not isinstance(close, list): ...`
  coercion guard and the `_orders`/`_plots`/`_alerts`/`_risk`/`_market_position`/`time_s`/
  `_first_bar` initialisers are consumed automatically (`_skip_prologue`).
- **Inputs** as `Name = kwargs.get('Name', <default>)`, key equal to target
  (`_parse_inputs`).
- **Vars/Arrays** as a contiguous block of initialisers to not-yet-declared names:
  `f32(<lit>)`, a bare literal, `[init]`, or `[init] * (size + 1)` (`_parse_decls`,
  `_array_init_form`).
- **Series access:** `series[-1]` (current bar) and `series[-(n + 1)]` (n bars back)
  (`_inv_subscript`).
- **Control flow:** `if/else`, `for i in range(int(a), int(b) + 1)` (inclusive `to`) and the
  `-1` step form (`downto`), `while <cond>`, the `while True: ...; if C: break` repeat-until
  shim, `break`, `continue`, and the `if _first_bar: ...; _first_bar = False` Once block
  (`_inv_if`, `_inv_for`, `_inv_while`).
- **Expressions:** numeric/string/boolean constants, names, unary `-`/`not`, `and`/`or`,
  `+ - * / % **`, comparisons, and the fixed call set `f32`, `int`, `abs`, `float`, `max`,
  `min`, `math.log/sqrt/ceil/floor/exp`, plus the `pl_*` runtime helpers and the string and
  date/time inline forms the forward codegen emits (`inv_expr`, `_inv_call`, `_inv_pl_call`).
- **Effects:** orders via `_orders.append((...))`, plots via `_plots.setdefault(...)
  .append(...)`, risk via `_risk['key'] = ...`, `print(...)`, `_alerts.append(...)`, and
  `pass  # Abort` (`_inv_expr_stmt`, `_inv_order`, `_inv_risk_assign`).

Anything else — a comprehension, a `numpy`/`pandas` call, a lambda, a `with`/`try` block, an
unrecognised function — raises `PyFrontError`. See the README **Limitations** section for the
residual honesty gaps (for example, an undeclared identifier can still slip through).

## Worked example

The dialect *is* the forward transpiler's own output, so the most dependable worked example
generates it from a shipping EasyLanguage indicator and reads it straight back:

```python
# 1. Produce mirror-dialect Python from a shipping EL indicator.
from pl_transpiler import transpile
el_src = open("ground_truth/GT1_functions_indicators.txt").read()
mirror_py = transpile(el_src, trace=False)
open("llm_dialect_example.py", "w", encoding="utf-8").write(mirror_py)
print("mirror-dialect Python:", len(mirror_py.splitlines()), "lines")

# 2. The front-end reads that exact dialect back into the shared IR.
from pl_transpiler.py_front import py_to_ast
ast_obj = py_to_ast(mirror_py)
print("parsed back to IR:", ast_obj["type"],
      "with", len(ast_obj["body"]), "top-level nodes")
```

That generated `.py` is accepted Python. Convert it straight back to canonical, compile-safe
EasyLanguage:

```bash
el_emit llm_dialect_example.py -o llm_dialect_example.el.txt
```

## The fail-loud contract

Idiomatic Python that an LLM would happily write — but that is outside the dialect — is
**rejected loudly**, never converted into plausible-but-wrong EasyLanguage:

```python
from pl_transpiler.py_front import py_to_ast, PyFrontError

# A 20-bar SMA the "obvious" Python way. sum()/slicing are NOT in the dialect.
off_dialect = (
    "def strategy(open, high, low, close, volume, date, time, **kwargs):\n"
    "    sma = sum(close[-20:]) / 20\n"
)
try:
    py_to_ast(off_dialect)
    raise SystemExit("BUG: expected PyFrontError, but the input was accepted")
except PyFrontError as e:
    print("fail-loud OK:", str(e).splitlines()[0])
```

The lesson the cheatsheet exists to teach: express the SMA the way EasyLanguage does
(declare a Var, accumulate across bars, or use a supported averaging helper) and let
PowerBridge emit the Python — do not hand the tool arbitrary Python and expect a guess.
