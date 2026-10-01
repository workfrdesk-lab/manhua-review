"""Canonical Story review state machine.

AI reconciliation may preserve an existing state, but only an explicit human
transition may change it through the API.
"""

from enum import Enum


class ReviewState(str, Enum):
    """The only persisted Story review states.

    Transition contract: every pair in :data:`VALID_REVIEW_TRANSITIONS` is
    allowed. A transition to the current state is idempotent; it is accepted
    without changing the record. Explicit human review may move between any
    two canonical states, including reopening or reversing a decision.
    """

    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


VALID_REVIEW_STATES = frozenset(ReviewState)

VALID_REVIEW_TRANSITIONS = {state: VALID_REVIEW_STATES for state in VALID_REVIEW_STATES}


def validate_review_transition(current: str | ReviewState, requested: str | ReviewState) -> str:
    try:
        current_state = ReviewState(current)
        requested_state = ReviewState(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Story review state") from exc
    if requested_state not in VALID_REVIEW_TRANSITIONS[current_state]:
        raise ValueError(f"invalid Story review transition: {current} -> {requested}")
    return requested_state.value
