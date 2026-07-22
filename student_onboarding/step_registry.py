"""
Registry of onboarding step definitions.

Host apps (e.g. cis) register steps at AppConfig.ready() so the
`student_onboarding` app doesn't need to know about domain concepts like
FERPA or tuition assistance. Everything that needs a list of step keys
(default-step seeding, the notify settings form, the backfill command)
reads from this registry.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class StepDefinition:
    key: str
    label: str
    order: int = 100
    url_name: str = ''
    message: str = ''
    # Predicate(student) -> bool. If set, the step is only seeded when True.
    # If None, the step is always seeded.
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
