#!/usr/bin/python3
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

"""Capture-free differential sweep of every EL source in ground_truth/.

The parent process never executes a source directly. It starts one worker for
IR/strict-transpile validation and one worker for each execution dialect. Every
worker is protected by an exec-preserved 60-second Perl alarm.
"""

import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GROUND_TRUTH = REPO / "ground_truth"
DEFAULT_BARS = REPO / "examples" / "NQ_sample_bars.csv"
PYTHON = sys.executable
# perl `alarm` gives a hard in-child timeout where available (POSIX); elsewhere
# (Windows) the parent communicate() timeout below is the only cap.
PERL = "/usr/bin/perl" if os.path.exists("/usr/bin/perl") else None
TIMEOUT_SECONDS = 60
PARENT_TIMEOUT_SECONDS = TIMEOUT_SECONDS
WORKER_MARKER = "GT_SWEEP_RESULT="
SOURCE_SUFFIXES = {".el", ".ela", ".txt"}

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _error_result(exc, phase):
    return {
        "status": "ERROR",
        "phase": phase,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _worker_transpile(source):
    from pl_transpiler import ir, transpile
    from pl_transpiler.reverse import parse_el

    try:
        ast = parse_el(source)
        payload = ir.serialise_ir(ast)
        if ir.deserialise_ir(payload) != ast:
            raise RuntimeError("IR serialisation changed the parsed AST")
    except Exception as exc:
        return _error_result(exc, "ir")

    try:
        traced_python = transpile(source, trace=True)
        untraced_python = transpile(source, persist=True)
        compile(traced_python, "<gt-sweep-traced>", "exec")
        compile(untraced_python, "<gt-sweep-untraced>", "exec")
    except Exception as exc:
        result = _error_result(exc, "transpile")
        result["ir"] = {"status": "OK", "bytes": len(payload.encode("utf-8"))}
        return result

    return {
        "status": "OK",
        "phase": "complete",
        "ir": {"status": "OK", "bytes": len(payload.encode("utf-8"))},
        "traced_python_bytes": len(traced_python.encode("utf-8")),
        "untraced_python_bytes": len(untraced_python.encode("utf-8")),
    }


def _execution_result(records):
    from pl_transpiler.tools.pl_oracle import fills_sha256

    fills = len(records)
    return {
        "status": "OK",
        "fills": fills,
        "round_turns": fills // 2,
        "digest": fills_sha256(records),
    }


def _worker_execute(source, bars_path, config_name, mode):
    from pl_transpiler.runtime.instrument_config import get_config
    from pl_transpiler.tools.pl_oracle import run_oracle
    from pl_transpiler.tools.pl_run import load_bars, run_el

    try:
        bars = load_bars(str(bars_path))
        config = get_config(config_name)
        if mode == "traced":
            output = run_el(
                source, bars, config, retain_rows=False)
            result = _execution_result(output["trades"])
        elif mode == "untraced":
            output = run_oracle(source, bars, config)
            result = {
                "status": "OK",
                "fills": output["fills"],
                "round_turns": output["round_turns"],
                "digest": output["sha256"],
            }
        else:
            raise ValueError("unknown execution mode: %s" % mode)
        result["bars"] = len(bars)
        return result
    except Exception as exc:
        return _error_result(exc, "execute")


def _worker_main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", required=True,
                        choices=("transpile", "traced", "untraced"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    try:
        source = Path(args.source).read_text(encoding="utf-8")
        if args.worker == "transpile":
            result = _worker_transpile(source)
        else:
            result = _worker_execute(
                source, Path(args.bars), args.config, args.worker)
    except Exception as exc:
        result = _error_result(exc, "worker_setup")

    print(WORKER_MARKER + json.dumps(result, sort_keys=True))
    return 0


def _trim_output(text, limit=4000):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... output truncated by gt_sweep ..."


def _run_capped_worker(source, bars, config_name, mode):
    command = ([PERL, "-e", "alarm 60; exec @ARGV", "--"] if PERL else []) + [
        PYTHON, str(Path(__file__).resolve()),
        "--worker", mode,
        "--source", str(source),
        "--bars", str(bars),
        "--config", config_name,
    ]
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    proc = subprocess.Popen(
        command,
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=PARENT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return {
            "status": "TIMEOUT",
            "phase": mode,
            "error": "parent safety timeout after %d seconds"
                     % PARENT_TIMEOUT_SECONDS,
        }

    sigalrm = getattr(signal, "SIGALRM", None)
    if sigalrm is not None and proc.returncode in (-sigalrm, 128 + sigalrm):
        return {
            "status": "TIMEOUT",
            "phase": mode,
            "error": "hard timeout after %d seconds" % TIMEOUT_SECONDS,
        }

    payload = None
    for line in reversed(stdout.splitlines()):
        if line.startswith(WORKER_MARKER):
            try:
                payload = json.loads(line[len(WORKER_MARKER):])
            except json.JSONDecodeError:
                payload = None
            break

    if proc.returncode != 0 or payload is None:
        return {
            "status": "ERROR",
            "phase": mode,
            "error_type": "WorkerProcessError",
            "error": "worker exited %d without a valid result" % proc.returncode,
            "stdout": _trim_output(stdout),
            "stderr": _trim_output(stderr),
        }
    if stderr.strip():
        payload["stderr"] = _trim_output(stderr)
    return payload


def _resolve_source(value):
    candidate = Path(value)
    if not candidate.is_absolute():
        repo_relative = REPO / candidate
        gt_relative = GROUND_TRUTH / candidate
        candidate = repo_relative if repo_relative.exists() else gt_relative
    candidate = candidate.resolve()
    try:
        candidate.relative_to(GROUND_TRUTH.resolve())
    except ValueError as err:
        raise ValueError("source is outside ground_truth/: %s" % value) from err
    if not candidate.is_file():
        raise ValueError("source does not exist: %s" % value)
    if candidate.suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("not an EL source suffix: %s" % value)
    return candidate


def _discover_sources(selected):
    if selected:
        return sorted(set(_resolve_source(value) for value in selected))
    return sorted(
        path.resolve()
        for path in GROUND_TRUTH.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
    )


def _comparison(traced, untraced):
    keys = ("fills", "round_turns", "digest")
    if traced.get("status") != "OK" or untraced.get("status") != "OK":
        return "DIVERGE", ["execution did not complete in both modes"]
    differences = [
        key for key in keys if traced.get(key) != untraced.get(key)
    ]
    if not differences:
        return "MATCH", []
    return "DIVERGE", ["traced and untraced differ on " + ", ".join(differences)]


def _source_result(source, bars, config_name):
    transpile_result = _run_capped_worker(
        source, bars, config_name, "transpile")
    traced = _run_capped_worker(source, bars, config_name, "traced")
    untraced = _run_capped_worker(source, bars, config_name, "untraced")
    comparison, reasons = _comparison(traced, untraced)

    if transpile_result.get("status") != "OK":
        reasons.insert(
            0, "strict transpile did not complete: "
            + transpile_result.get("status", "ERROR"))
        comparison = "DIVERGE"

    return {
        "source": source.relative_to(REPO).as_posix(),
        "transpiles_clean": transpile_result.get("status") == "OK",
        "transpile": transpile_result,
        "executes_at_all": (
            traced.get("status") == "OK" or untraced.get("status") == "OK"),
        "executes_both": (
            traced.get("status") == "OK" and untraced.get("status") == "OK"),
        "traced": traced,
        "untraced": untraced,
        "comparison": comparison,
        "finding": comparison == "DIVERGE",
        "finding_reasons": reasons,
    }


def _summary(results):
    return {
        "sources": len(results),
        "transpiles_clean": sum(
            1 for result in results if result["transpiles_clean"]),
        "executes_at_all": sum(
            1 for result in results if result["executes_at_all"]),
        "executes_both": sum(
            1 for result in results if result["executes_both"]),
        "match": sum(
            1 for result in results if result["comparison"] == "MATCH"),
        "diverge": sum(
            1 for result in results if result["comparison"] == "DIVERGE"),
        "timeouts": sum(
            1
            for result in results
            for phase in ("transpile", "traced", "untraced")
            if result[phase].get("status") == "TIMEOUT"
        ),
    }


def _finding_record(result):
    return {
        "source": result["source"],
        "reasons": result["finding_reasons"],
        "transpile_status": result["transpile"]["status"],
        "traced_status": result["traced"]["status"],
        "traced_fills": result["traced"].get("fills"),
        "traced_round_turns": result["traced"].get("round_turns"),
        "traced_digest": result["traced"].get("digest"),
        "untraced_status": result["untraced"]["status"],
        "untraced_fills": result["untraced"].get("fills"),
        "untraced_round_turns": result["untraced"].get("round_turns"),
        "untraced_digest": result["untraced"].get("digest"),
    }


def _cell(result):
    if result.get("status") == "OK":
        return str(result["fills"])
    return result.get("status", "ERROR")


def _transpile_cell(result):
    status = result.get("status", "ERROR")
    return status if status in ("OK", "TIMEOUT") else "FAIL"


def _print_table(results, summary):
    name_width = max(
        [len("SOURCE")] + [len(result["source"]) for result in results])
    print(
        "%-*s  %-9s  %-12s  %-14s  %s"
        % (name_width, "SOURCE", "TRANSPILE", "TRACED FILLS",
           "UNTRACED FILLS", "RESULT"))
    print("-" * (name_width + 57))
    for result in results:
        print(
            "%-*s  %-9s  %-12s  %-14s  %s"
            % (
                name_width,
                result["source"],
                _transpile_cell(result["transpile"]),
                _cell(result["traced"]),
                _cell(result["untraced"]),
                result["comparison"],
            )
        )
    print(
        "\nSUMMARY: %(sources)d sources, %(transpiles_clean)d transpile clean, "
        "%(executes_both)d execute both, %(match)d MATCH, %(diverge)d DIVERGE, "
        "%(timeouts)d TIMEOUT" % summary)


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Capture-free traced/untraced differential sweep over "
                    "ground_truth/.")
    parser.add_argument(
        "--out", required=True,
        help="JSON report path (parent directories are created).")
    parser.add_argument(
        "--source", action="append", default=[],
        help="limit the sweep to this ground_truth source; repeatable.")
    parser.add_argument(
        "--bars", default=str(DEFAULT_BARS),
        help="capture-free OHLCV tape (default: examples/NQ_sample_bars.csv).")
    parser.add_argument(
        "--config", default="NQ",
        help="instrument config (default: NQ).")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        return _worker_main(argv)

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        sources = _discover_sources(args.source)
    except ValueError as exc:
        parser.error(str(exc))
    if not sources:
        parser.error("no EL sources found")

    bars = Path(args.bars)
    if not bars.is_absolute():
        bars = REPO / bars
    bars = bars.resolve()
    if not bars.is_file():
        parser.error("bars file does not exist: %s" % bars)

    results = []
    for source in sources:
        results.append(_source_result(source, bars, args.config))

    summary = _summary(results)
    report = {
        "schema_version": 1,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "scope": "capture-free ground_truth differential sweep",
        "bars": bars.relative_to(REPO).as_posix()
                if REPO in bars.parents else str(bars),
        "config": args.config,
        "timeout_seconds_per_worker": TIMEOUT_SECONDS,
        "summary": summary,
        "sources": results,
        "findings": [
            _finding_record(result) for result in results if result["finding"]
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(str(temporary), str(out_path))

    _print_table(results, summary)
    print("REPORT: %s" % out_path)
    return 1 if summary["diverge"] else 0


if __name__ == "__main__":
    sys.exit(main())
