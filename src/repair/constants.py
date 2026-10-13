"""Shared defaults for the repair layer.

`DEFAULT_REPAIR_TIMEOUT_S` was defined verbatim in seven modules and consumed
in one; the other six were copies that would have drifted apart silently.
"""

from __future__ import annotations

DEFAULT_REPAIR_TIMEOUT_S = 180
