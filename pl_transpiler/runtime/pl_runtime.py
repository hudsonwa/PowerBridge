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

"""
PowerLanguage runtime helpers.
These are called by transpiled Python code at execution time.

This module is the facade: the code lives in the ``_rt_*`` submodules and every
public name is re-exported here, because generated strategies do
``from pl_transpiler.runtime.pl_runtime import *``. There is deliberately no
``__all__`` (codegen derives its bare-name set from ``dir()`` of this module).
"""

from ._rt_core import *  # noqa: F401,F403
from ._rt_indicators import *  # noqa: F401,F403
from ._rt_trend import *  # noqa: F401,F403
from ._rt_drawing import *  # noqa: F401,F403
from ._rt_arrays_dates import *  # noqa: F401,F403
from ._rt_builtins_az import *  # noqa: F401,F403
from ._rt_datetime import *  # noqa: F401,F403
from ._rt_session import *  # noqa: F401,F403
from ._rt_color import *  # noqa: F401,F403
from ._rt_platform_stubs import *  # noqa: F401,F403

import os as _os  # noqa: F401  (same module object _rt_core uses)

# Private helpers and state that tests, builtins and tools reach through here.
from ._rt_core import (  # noqa: F401
    _CFloat, _UNIMPL_COSMETIC,
    _UNIMPL_COSMETIC_PREFIXES, _UNIMPL_STR_RETURNING, _UNIMPL_WHITELIST,
    _current_run_state, _run_state, _unimplemented, _strict_from_env,
)
from ._rt_indicators import (  # noqa: F401
    _adx_engine, _calc_dmi, _compute_adx_series, _cross_prior_relation, _ema,
    _find_first_valid, _ind_cache, _pb_lag, _linreg_window, _tr_rma, _true_range_for_adx,
    _valid_window,
)
from ._rt_builtins_az import (  # noqa: F401
    _pl_array_getval, _pl_array_setval,
)
from ._rt_datetime import (  # noqa: F401
    _decode_dt_yyyymmdd, _fmt_dt, _ole_from_ymd, _parse_dt_formatted,
    _parse_time_frac, _ymd_from_ole,
)
from ._rt_core import _install_loud_stubs

# MUST stay last: wraps the trivial pl_* stubs only after everything is bound.
_install_loud_stubs()
