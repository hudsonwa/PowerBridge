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

"""Portable integration gate for the capture-free ground-truth sweep."""

import json
import os
import subprocess
import sys
import tempfile


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SWEEP = os.path.join(REPO, "tools", "gt_sweep.py")
SUBSET = [
    "ground_truth/GT1_functions_indicators.txt",
    "ground_truth/GT2_strategy_orders_position.txt",
]


def test_capture_free_subset_matches():
    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "report.json")
        command = [PYTHON, SWEEP, "--out", report_path]
        for source in SUBSET:
            command.extend(["--source", source])
        proc = subprocess.run(
            command,
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, output

        with open(report_path, encoding="utf-8") as report_file:
            report = json.load(report_file)
        assert report["summary"]["sources"] == len(SUBSET), report["summary"]
        assert report["summary"]["transpiles_clean"] == len(SUBSET), report
        assert report["summary"]["diverge"] == 0, report["findings"]
        assert report["findings"] == []
        assert [row["source"] for row in report["sources"]] == SUBSET
        assert all(row["comparison"] == "MATCH"
                   for row in report["sources"]), report["sources"]

    print(output.rstrip())
    print("GT SWEEP SUBSET PASSED: 2 sources, 0 DIVERGE")


def main():
    test_capture_free_subset_matches()
    return 0


if __name__ == "__main__":
    sys.exit(main())
