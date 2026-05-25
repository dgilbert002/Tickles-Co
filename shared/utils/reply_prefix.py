"""
Module: shared.utils.reply_prefix
Purpose: Single canonical implementation of Discord ``[Reply to @user]: ...``
         prefix stripping. Imported by all layers (enrichment, intelligence,
         dashboard, postmortem, news provider, signal extractor).

This consolidates the previously-triplicated copies of ``strip_reply_prefix``:
  * shared/intelligence/text_signal_extractor.py (pre-fix canonical)
  * shared/enrichment/stages/symbol_resolver.py  (Bug C inline copy)
  * (third copy elsewhere — captured by 2nd-round audit)

Each downstream module imports the shared helper instead of carrying its
own copy. ``shared/utils/`` is the lowest layer, so importing from it does
not break layering for any caller.

Background:
  Discord clients send a "reply" as a concatenated message that begins with
  ``[Reply to @parent]: <quoted parent text>`` followed by the actual reply
  body on the next line. Naïve text parsers (regex symbol extraction,
  sentiment/relevance/language detectors, vision LLM ``news_context``,
  postmortem narrative inputs, news-feed display, etc.) would interpret
  the parent's text as if it were the trader's own — leading to bogus
  symbol mentions, polluted sentiment scores, false relevance hits, and
  generally nonsensical analysis chains. Bug 12 + sibling Bug C identified
  the surface; subsequent audit found seven more leak sites which all use
  this helper.
"""

from __future__ import annotations

__all__ = ["strip_reply_prefix"]


def strip_reply_prefix(text: str) -> str:
    """Return ``text`` with the leading ``[Reply to @user]: ...`` line stripped.

    The Discord reply prefix has the form::

        [Reply to @username]: quoted parent text...
        actual reply body...

    Only the first line is removed. If ``text`` does not start with the
    reply prefix (most messages), the original string is returned
    unchanged. Empty / falsy inputs return an empty string so callers can
    safely concatenate.

    Args:
        text: Raw message body, may be ``None`` or empty.

    Returns:
        The reply body without the parent quote, or the unchanged input
        when no prefix is present.
    """
    if not text:
        return ""
    if text.startswith("[Reply to @"):
        parts = text.split("\n", 1)
        return parts[1] if len(parts) > 1 else ""
    return text
