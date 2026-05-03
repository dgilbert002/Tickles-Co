"""
Module: anchors
Purpose: Stable URL-fragment contract for every dashboard card.
Location: /opt/tickles/shared/dashboard/anchors.py

A user pasting a URL with `#pos-12345` MUST land on position 12345 with the card
expanded and visually highlighted. Anchor format is intentionally short to fit
in chat messages.
"""

from typing import Literal

AnchorKind = Literal["sig", "pos", "opn", "pm", "trade", "interp"]


def anchor_for(kind: AnchorKind, id_: int | str) -> str:
    """Return the canonical fragment for a given entity.

    Args:
        kind: The type of entity (sig, pos, opn, pm, trade, interp).
        id_: The primary key or unique identifier.

    Returns:
        A string like '#pos-12345'.
    """
    return f"#{kind}-{id_}"
