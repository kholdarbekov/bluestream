"""The one rendering of the "title + full address" block.

`address_block_markdown_v2` serves the checkout card and order details, which are sent with
`ParseMode.MARKDOWN_V2` (incident 2026-10-18: a raw backtick in a geocoded address made Telegram
refuse the card). `address_lines` serves the plain-text confirmation screen (Task 21), which has
no `parse_mode`. Both read `_kept_parts`, the single rule for which parts show and in what order.
"""

from typing import Optional

from telegram.helpers import escape_markdown


def _kept_parts(title: Optional[str], full_address: Optional[str]) -> list[tuple[str, bool]]:
    """Each non-empty part, title first, flagged True when it is the title."""
    parts = []
    if title:
        parts.append((title, True))
    if full_address:
        parts.append((full_address, False))
    return parts


def address_lines(title: Optional[str], full_address: Optional[str]) -> str:
    """Title over full address, unescaped, for messages sent without a parse_mode."""
    return "\n".join(value for value, _ in _kept_parts(title, full_address))


def address_block_markdown_v2(title: Optional[str], full_address: Optional[str]) -> str:
    """Bold title over full address, every part escaped for MarkdownV2."""
    lines = []
    for value, is_title in _kept_parts(title, full_address):
        escaped = escape_markdown(value, version=2)
        lines.append(f"*{escaped}*" if is_title else escaped)
    return "\n".join(lines)
