"""
Registry of onboarding step definitions.

Host apps (e.g. cis) register steps at AppConfig.ready() so the
`student_onboarding` app doesn't need to know about domain concepts like
FERPA or tuition assistance. Everything that needs a list of step keys
(default-step seeding, the notify settings form, the backfill command)
reads from this registry.
"""
import inspect
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def _predicate_accepts_term(predicate):
    """Whether a predicate takes the optional second `term` argument.

    Tenant catalogs declare `seeded_when` as either `(student)` or
    `(student, term=None)`; passing `term` positionally to a one-argument
    predicate raises TypeError (package-cis#7). Cached per function object:
    the registry is small and fixed, so this resolves once per predicate.
    """
    try:
        parameters = inspect.signature(predicate).parameters.values()
    except (TypeError, ValueError):
        # No introspectable signature: one argument is the safer convention.
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in parameters):
        return True
    positional = [
        p for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _call_predicate(predicate, student, term):
    if _predicate_accepts_term(predicate):
        return predicate(student, term)
    return predicate(student)


@dataclass
class StepDefinition:
    key: str
    label: str
    order: int = 100
    url_name: str = ''
    message: str = ''
    # Predicate(student, term=None) -> bool. If set, the step is only seeded
    # when True. If None, the step is always seeded. `term` is passed through
    # from the caller's already-resolved active_term() when available;
    # predicates that don't need it may ignore the parameter.
    seeded_when: Optional[Callable] = None
    # Predicate(student, term) -> bool. Used by the backfill command to decide
    # whether an existing student should have this step pre-marked complete.
    complete_when: Optional[Callable] = None
    # Message rendered when the step is auto-completed.
    complete_message: str = ''
    # Short description shown on the admin notification settings page. If blank,
    # the step is not offered as a notification option.
    notify_label: str = ''
    # Optional side effect fired by the pending-onboarding notifier when this
    # step is pending for a student. Signature: fn(student, term) -> None.
    # Keeps domain-specific notifications (e.g. parent consent) out of the
    # generic notifier — the callable lives in the tenant catalog.
    notify_action: Optional[Callable] = None

    def should_seed(self, student, term) -> bool:
        """Whether this step belongs on the student's checklist for `term`.

        True when `seeded_when` is unset. One-argument predicates are called
        without `term`. A raising predicate propagates; callers decide how to
        degrade.
        """
        if not self.seeded_when:
            return True
        return bool(_call_predicate(self.seeded_when, student, term))

    def is_complete(self, student, term) -> Optional[bool]:
        """Whether `complete_when` says the step is done for `term`.

        None when the step has no `complete_when` (only events complete it) or
        when the predicate raises (logged), so callers leave the step alone.
        """
        if not self.complete_when:
            return None
        try:
            return bool(_call_predicate(self.complete_when, student, term))
        except Exception:
            logger.exception(
                'complete_when for onboarding step %r raised for student %s',
                self.key, getattr(student, 'pk', student),
            )
            return None


_registry: Dict[str, StepDefinition] = {}


def register(step: StepDefinition) -> None:
    """Idempotent — later registrations with the same key overwrite."""
    _registry[step.key] = step


def all_steps() -> List[StepDefinition]:
    return sorted(_registry.values(), key=lambda s: (s.order, s.key))


def get(key: str) -> Optional[StepDefinition]:
    return _registry.get(key)


def notifiable_steps() -> List[StepDefinition]:
    """Steps that admins can toggle on the notification settings page."""
    return [s for s in all_steps() if s.notify_label]
