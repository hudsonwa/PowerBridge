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

"""O(1)-per-bar lookback history for persist-mode user variables.

Persist codegen passes a user variable to a series function as
``(_state['x'] + [x])``: its history plus the current bar's value. On a plain
list that concatenation copies the whole history every bar, so a run costs
O(bars^2). The bar loop instead hands the strategy a ``SeriesState``, which
stores each seeded history as a ``SeriesHistory``. That list's ``+ [x]``
returns a ``SeriesView``: a read-only snapshot of the first ``n`` items plus
``x``, built in O(1). Reads (len, indexing, slicing, iteration, equality)
return exactly what the concatenated list would, and later appends to the
history never show through an existing view.

Not star-exported by pl_runtime, so these names never become EL bare names.
"""

from itertools import chain, islice


class SeriesView:
    """Read-only ``base[:n] + [last]``, without the copy."""

    __slots__ = ('_base', '_n', '_last')

    def __init__(self, base, n, last):
        self._base = base
        self._n = n
        self._last = last

    def __len__(self):
        return self._n + 1

    def __getitem__(self, i):
        n = self._n
        if isinstance(i, slice):
            start, stop, step = i.indices(n + 1)
            if step != 1:
                return [self[j] for j in range(start, stop, step)]
            if start >= stop:
                return []
            if stop <= n:
                return self._base[start:stop]
            out = self._base[start:n]
            out.append(self._last)
            return out
        if i < 0:
            i += n + 1
        if i == n:
            return self._last
        if 0 <= i < n:
            return self._base[i]
        raise IndexError('list index out of range')

    def __iter__(self):
        return chain(islice(self._base, self._n), (self._last,))

    def __reversed__(self):
        return reversed(self.tolist())

    def __contains__(self, v):
        return any(x is v or x == v for x in self)

    def __eq__(self, other):
        if isinstance(other, (SeriesView, list)):
            return self.tolist() == list(other)
        return NotImplemented

    def __ne__(self, other):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    __hash__ = None

    def __add__(self, other):
        return self.tolist() + other

    def __radd__(self, other):
        if isinstance(other, list):
            return other + self.tolist()
        return NotImplemented

    def __mul__(self, k):
        return self.tolist() * k

    __rmul__ = __mul__

    def index(self, *args):
        return self.tolist().index(*args)

    def count(self, v):
        return self.tolist().count(v)

    def tolist(self):
        out = self._base[:self._n]
        out.append(self._last)
        return out

    def __repr__(self):
        return repr(self.tolist())


class SeriesHistory(list):
    """Append-only history list whose ``+ [x]`` is an O(1) ``SeriesView``."""

    __slots__ = ()

    def __add__(self, other):
        if type(other) is list and len(other) == 1:
            return SeriesView(self, len(self), other[0])
        return list.__add__(self, other)


class SeriesState(dict):
    """The bar loop's ``_state``: stores each list seeded before
    ``__pb_seeded`` is set (the lookback histories) as a ``SeriesHistory``.
    Internal ``__pb_*`` keys and later stores (arrays) are kept as given."""

    __slots__ = ()

    def __setitem__(self, key, value):
        if (type(value) is list and '__pb_seeded' not in self
                and not key.startswith('__pb_')):
            value = SeriesHistory(value)
        dict.__setitem__(self, key, value)
