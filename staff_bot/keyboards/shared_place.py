"""Keyboards for the shared-place question on the driver's Delivered screen.

Every callback string is spelled out with a LITERAL suffix. The wiring-contract
checker expands f-strings to sample callback data and matches them against the
registered patterns, so a suffix hidden behind a variable (`_{action}`) cannot be
sampled and its button would go unchecked.
"""

from typing import Any, List, Literal, Mapping, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.i18n import i18n

# A blank name still has to be a readable label.
BLANK_NAME = "—"


def display_name(name: Any) -> str:
    """The name as typed, or an em dash when it is blank. NOT escaped: buttons are not HTML."""
    text = str(name).strip() if name is not None else ""
    return text or BLANK_NAME


class SharedPlaceKeyboards:
    """Inline keyboards for the shared-place question."""

    @staticmethod
    def question(case_id: int, candidates: Sequence[Mapping[str, Any]], language: str) -> InlineKeyboardMarkup:
        """One candidate per row, then one row with `None of these` and `Skip`."""
        rows: List[List[InlineKeyboardButton]] = []
        for candidate in candidates:
            i = int(candidate["index"])
            label = i18n.get(
                "staff.delivery.shared_place.candidate",
                language,
                name=display_name(candidate.get("first_name")),
                empties=candidate.get("empties", 0),
                metres=candidate.get("distance_m", 0),
            )
            rows.append([InlineKeyboardButton(label, callback_data=f"staff_sp_{case_id}_p{i}")])
        rows.append(
            [
                InlineKeyboardButton(
                    i18n.get("staff.delivery.shared_place.none", language),
                    callback_data=f"staff_sp_{case_id}_none",
                ),
                InlineKeyboardButton(
                    i18n.get("staff.delivery.shared_place.skip", language),
                    callback_data=f"staff_sp_{case_id}_skip",
                ),
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def skip_only(case_id: int, action: Literal["skip", "done"], language: str) -> InlineKeyboardMarkup:
        """A single Skip button: `skip` files a skipped answer, `done` closes a note prompt."""
        label = i18n.get("staff.delivery.shared_place.skip", language)
        if action == "done":
            callback_data = f"staff_sp_{case_id}_done"
        else:
            callback_data = f"staff_sp_{case_id}_skip"
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])
