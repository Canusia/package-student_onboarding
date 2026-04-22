"""
Public helpers for managing per-term student onboarding state.

All counter mutations run inside a transaction with select_for_update on
the parent row so concurrent signal handlers don't corrupt total_steps /
completed_steps.
"""
from django.db import transaction
from django.utils import timezone

from cis.utils import active_term

from .models import StudentOnboarding, StudentOnboardingStep


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
