"""Shared-place request bodies and query strings (spec 2026-11-10 §4.4, §4.5; §9 binds). Request models only.

The shape rules that need the case (the index within the frozen list, the note's length) live in
`SharedPlaceDriverStep`; these models only coerce the JSON and refuse unknown keys. Likewise the resolve rules that
need the case or live state (a move within the source's empties, a linked account) live in `SharedPlaceResolve`.
"""

from typing import Annotated, Dict, FrozenSet, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, constr, model_validator

from business_app.models.shared_place_case import SHARED_PLACE_CHOICE_KINDS
from business_app.serializers.sales_serializers import PageQuery


class SharedPlaceAnswerRequest(BaseModel):
    """`PUT /staff/shared-place-cases/<id>/answer`."""

    model_config = ConfigDict(extra="forbid")

    answer: Literal["picked", "none", "skipped"]
    candidate_index: Optional[StrictInt] = None
    note: Optional[StrictStr] = None


class SharedPlaceNoteRequest(BaseModel):
    """`PUT /staff/shared-place-cases/<id>/note`."""

    model_config = ConfigDict(extra="forbid")

    note: StrictStr


class SharedPlaceCaseListQuery(PageQuery):
    """`GET /admin/shared-place-cases`'s query string, typed once.

    `page`/`per_page` clamp through `PageQuery`. An unknown status is a pydantic error, which `handle_api_exception`
    answers with 400.
    """

    status: Literal["open", "resolved", "closed"] = "open"


# The optional fields each choice takes (the Shared contracts' resolve payload), keyed by every one of
# SHARED_PLACE_CHOICE_KINDS (pinned by tests/unit/test_shared_place_choice_kinds.py). A field sent with a kind that does
# not take it is a bad shape, so a stored claim never carries a value nothing reads. C's source goes with its count:
# a C without a count is link only and moves nothing, so it names no source.
_RESOLVE_FIELDS_BY_KIND: Dict[str, FrozenSet[str]] = {
    "shared_place": frozenset({"target_address_id"}),
    "same_home": frozenset({"target_address_id", "target_user_id"}),
    "other_place": frozenset({"target_user_id", "move_count", "source_address_id"}),
    "own_bottles": frozenset(),
    "wrong_count": frozenset({"actual_returned"}),
    "close": frozenset(),
}
_RESOLVE_OPTIONAL_FIELDS = frozenset().union(*_RESOLVE_FIELDS_BY_KIND.values())


class SharedPlaceResolveRequest(BaseModel):
    """`POST /admin/shared-place-cases/<id>/resolve`: one of the six choices, its mandatory reason, and the fields
    that choice takes. The reason is trimmed, then capped at 500 characters."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[SHARED_PLACE_CHOICE_KINDS]
    reason: constr(strict=True, strip_whitespace=True, min_length=1, max_length=500)
    target_address_id: Optional[StrictInt] = None
    target_user_id: Optional[StrictInt] = None
    move_count: Optional[Annotated[StrictInt, Field(ge=1)]] = None
    source_address_id: Optional[StrictInt] = None
    actual_returned: Optional[Annotated[StrictInt, Field(ge=0)]] = None

    @model_validator(mode="after")
    def _only_the_fields_of_the_kind(self) -> "SharedPlaceResolveRequest":
        takes = _RESOLVE_FIELDS_BY_KIND[self.kind]
        if self.move_count is None:
            takes = takes - {"source_address_id"}
        stray = sorted(name for name in _RESOLVE_OPTIONAL_FIELDS - takes if getattr(self, name) is not None)
        if stray:
            raise ValueError(f"{self.kind} takes no {', '.join(stray)}")
        return self
