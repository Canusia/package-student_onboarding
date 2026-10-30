"""
Public helpers for managing per-term student onboarding state.

All counter mutations run inside a transaction with select_for_update on
the parent row so concurrent signal handlers don't corrupt total_steps /
completed_steps.
"""
import logging

from django.db import transaction
from django.utils import timezone

from cis.utils import active_term

from .models import StudentOnboarding, StudentOnboardingStep

logger = logging.getLogger(__name__)

_DONE_STATUSES = (
    StudentOnboardingStep.STATUS_COMPLETED,
    StudentOnboardingStep.STATUS_NOT_APPLICABLE,
)


def _resolve_term(term):
    return term or active_term()


def get_or_create_for_current_term(student, term=None):
    term = _resolve_term(term)
    onboarding, _ = StudentOnboarding.objects.get_or_create(
        student=student, term=term
    )
    return onboarding


def add_step(student, *, key, label, url_name='', message='', order=100,
             status=StudentOnboardingStep.STATUS_PENDING, term=None):
    """Idempotent: never duplicates (student, term, key).
    Increments parent.total_steps only on actual create."""
    term = _resolve_term(term)
    with transaction.atomic():
        onboarding = (
            StudentOnboarding.objects
            .select_for_update()
            .get_or_create(student=student, term=term)[0]
        )
        step, created = StudentOnboardingStep.objects.get_or_create(
            onboarding=onboarding,
            key=key,
            defaults={
                'label': label,
                'url_name': url_name,
                'message': message,
                'order': order,
                'status': status,
            },
        )
        if created:
            onboarding.total_steps = onboarding.steps.count()
            if status in (StudentOnboardingStep.STATUS_COMPLETED,
                          StudentOnboardingStep.STATUS_NOT_APPLICABLE):
                onboarding.completed_steps = (
                    onboarding.steps.filter(
                        status__in=[
                            StudentOnboardingStep.STATUS_COMPLETED,
                            StudentOnboardingStep.STATUS_NOT_APPLICABLE,
                        ]
                    ).count()
                )
                step.completed_on = timezone.now()
                step.save(update_fields=['completed_on'])
            onboarding.save(update_fields=['total_steps', 'completed_steps'])
            onboarding.recompute_completion()
    return step


def complete_step(student, *, key, message=None, term=None):
    return _transition_step(
        student, key=key, term=term, message=message,
        new_status=StudentOnboardingStep.STATUS_COMPLETED,
    )


def mark_not_applicable(student, *, key, term=None):
    return _transition_step(
        student, key=key, term=term, message=None,
        new_status=StudentOnboardingStep.STATUS_NOT_APPLICABLE,
    )


def _transition_step(student, *, key, term, message, new_status):
    term = _resolve_term(term)
    with transaction.atomic():
        try:
            onboarding = (
                StudentOnboarding.objects
                .select_for_update()
                .get(student=student, term=term)
            )
        except StudentOnboarding.DoesNotExist:
            return None
        try:
            step = onboarding.steps.get(key=key)
        except StudentOnboardingStep.DoesNotExist:
            return None

        was_done = step.is_done
        step.status = new_status
        step.completed_on = timezone.now()
        if message is not None:
            step.message = message
        step.save(update_fields=['status', 'completed_on', 'message'])

        if not was_done:
            onboarding.completed_steps = onboarding.steps.filter(
                status__in=[
                    StudentOnboardingStep.STATUS_COMPLETED,
                    StudentOnboardingStep.STATUS_NOT_APPLICABLE,
                ]
            ).count()
            onboarding.save(update_fields=['completed_steps'])
            onboarding.recompute_completion()
    return step


def reopen_step(student, *, key, term=None):
    """Move a completed step back to pending.

    Clears `completed_on`, restores the step definition's pending message (the
    completion message no longer applies), recounts `completed_steps` and
    recomputes the parent's completion. A `not_applicable` or already pending
    step is returned unchanged; a missing record or step returns None.
    """
    from .step_registry import get as get_definition

    term = _resolve_term(term)
    with transaction.atomic():
        try:
            onboarding = (
                StudentOnboarding.objects
                .select_for_update()
                .get(student=student, term=term)
            )
        except StudentOnboarding.DoesNotExist:
            return None
        try:
            step = onboarding.steps.get(key=key)
        except StudentOnboardingStep.DoesNotExist:
            return None
        if step.status != StudentOnboardingStep.STATUS_COMPLETED:
            return step

        step.status = StudentOnboardingStep.STATUS_PENDING
        step.completed_on = None
        definition = get_definition(key)
        if definition is not None:
            step.message = definition.message
        step.save(update_fields=['status', 'completed_on', 'message'])

        onboarding.completed_steps = onboarding.steps.filter(
            status__in=_DONE_STATUSES).count()
        onboarding.save(update_fields=['completed_steps'])
        onboarding.recompute_completion()
    return step


def _still_due(step, student, term):
    try:
        return step.should_seed(student, term)
    except Exception:
        logger.exception(
            'seeded_when for onboarding step %r raised for student %s',
            step.key, student.pk,
        )
        return False


def recalculate(student, *, term=None):
    """Bring the student's checklist for `term` (default: the active term) in
    line with the registered step predicates.

    - Missing steps whose `should_seed` is True are added.
    - A pending step whose `complete_when` is now True is completed.
    - A completed step whose `complete_when` is now False is reopened only
      when the step is `reopenable` and `should_seed` still holds.
    - Steps without `complete_when`, `not_applicable` steps and rows whose key
      is no longer registered are left alone.

    Returns {'seeded': [...], 'completed': [...], 'reopened': [...]} (step
    keys). With no active term nothing is created and the lists are empty.
    """
    from .step_registry import all_steps

    result = {'seeded': [], 'completed': [], 'reopened': []}
    term = _resolve_term(term)
    if term is None:
        return result

    steps = all_steps()
    onboarding = get_or_create_for_current_term(student, term=term)
    rows = {row.key: row.status for row in onboarding.steps.all()}

    for step in steps:
        if step.key in rows:
            continue
        try:
            due = step.should_seed(student, term)
        except Exception:
            logger.exception(
                'seeded_when for onboarding step %r raised for student %s',
                step.key, student.pk,
            )
            continue
        if not due:
            continue
        add_step(
            student, key=step.key, label=step.label, url_name=step.url_name,
            message=step.message, order=step.order, term=term,
        )
        rows[step.key] = StudentOnboardingStep.STATUS_PENDING
        result['seeded'].append(step.key)

    for step in steps:
        status = rows.get(step.key)
        if status not in (StudentOnboardingStep.STATUS_PENDING,
                          StudentOnboardingStep.STATUS_COMPLETED):
            continue
        done = step.is_complete(student, term)
        if done is None:
            continue
        if done and status == StudentOnboardingStep.STATUS_PENDING:
            complete_step(student, key=step.key,
                          message=step.complete_message or None, term=term)
            result['completed'].append(step.key)
        elif (not done and status == StudentOnboardingStep.STATUS_COMPLETED
              and step.reopenable and _still_due(step, student, term)):
            reopen_step(student, key=step.key, term=term)
            result['reopened'].append(step.key)
    return result
