"""Strategies package — aggregates every strategy module."""
from __future__ import annotations

from typing import Callable, Dict, List

from .single_indicator import STRATEGIES as _SI_STRATS

# Future: from .combo import STRATEGIES as _COMBO_STRATS
STRATEGIES: Dict[str, Callable] = {**_SI_STRATS}


def get(name: str) -> Callable:
    return STRATEGIES[name]


def list_all() -> List[str]:
    return sorted(STRATEGIES.keys())
