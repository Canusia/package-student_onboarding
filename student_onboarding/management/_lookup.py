"""Shared helpers for the onboarding management commands."""
from django.core.management.base import CommandError

from cis.models.student import Student


def resolve_student(value):
    """Look up a Student by UUID or email. Raises CommandError with a helpful
    message if the student is missing or the value is ambiguous."""
    if not value:
        raise CommandError('No student identifier supplied.')

    if '@' in value:
        try:
            return Student.objects.get(user__email=value)
        except Student.DoesNotExist:
            raise CommandError(f'No student found with email {value!r}.')
        except Student.MultipleObjectsReturned:
            raise CommandError(
                f'Multiple students share email {value!r}; use the UUID instead.'
            )

    try:
        return Student.objects.get(pk=value)
    except Student.DoesNotExist:
        raise CommandError(f'No student found with id {value!r}.')
    except (ValueError, TypeError):
        raise CommandError(
            f'{value!r} is neither a valid UUID nor an email address.'
        )
