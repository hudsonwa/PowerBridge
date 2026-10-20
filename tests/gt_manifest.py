#!/usr/bin/env python3
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

"""gt_manifest.py — the SINGLE canonical GT label -> (source_file, capture_file) map.

tests/run_gt.py and tests/golden_diff.py used to each carry a byte-identical copy of
this dict (the "did I update both?" hazard). They now both import it from here, but
each keeps an INDEPENDENT module-level dict object (``GT_FILES = dict(gt_manifest.GT_FILES)``):
the reverse gates monkeypatch ``run_gt.GT_FILES`` and ``golden_diff.GT_FILES``
SEPARATELY and their save/restore assumes two distinct objects — aliasing one shared
dict would silently change patch semantics.

Pure data: no imports, so this module is importable with only ``tests/`` on ``sys.path``.
"""

# GT label -> (source_file, capture_file), both relative to ground_truth/.
GT_FILES = {
    "GT1": ("GT1_functions_indicators.txt", "GT1_funcs.csv"),
    "GT2": ("GT2_strategy_orders_position.txt", "GT2_strategy.csv"),
    "GT3": ("GT3_coverage_indicator.txt", "GT3_coverage.csv"),
    "GT4": ("GT4_coverage_indicator.txt", "GT4_coverage.csv"),
    "GT_master": ("GT_master_strategy.txt", "GT_master_m0_2.csv"),
    "GTA5": ("GTA5_strategy.txt", "GTA5_o0_mm0_v3.csv"),
    # GTA5 OrderMode 1-4 (limit / stop / on-close / named-exit entry fills). Same
    # calc battery as GTA5_o0; the matrix source bakes OrderMode(N) as the input
    # default so only that order block fires. Paired with the fresh 2024-2025 data.
    "GTA5_o1": ("matrix/GTA5_o1_mm0.txt", "GTA5_o1_mm0.csv"),
    "GTA5_o2": ("matrix/GTA5_o2_mm0.txt", "GTA5_o2_mm0.csv"),
    "GTA5_o3": ("matrix/GTA5_o3_mm0.txt", "GTA5_o3_mm0.csv"),
    "GTA5_o4": ("matrix/GTA5_o4_mm0.txt", "GTA5_o4_mm0.csv"),
    # GTA6 is split across TWO capture files (column-limit safety). Same script,
    # two labels: _1 = file1 (calc/data/array/date/session/bar-metadata, 85 cols),
    # _2 = file2 (stats/trade-introspection/entry-exit, 74 cols).
    "GTA6_1": ("GTA6_strategy.txt", "GTA6_o0_mm0.csv"),
    "GTA6_2": ("GTA6_strategy.txt", "GTA6_o0_mm0_b.csv"),
}
