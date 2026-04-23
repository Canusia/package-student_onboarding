"""Manually mark an onboarding step completed / not_applicable for a student.

Escape hatch for when the normal event pipeline missed a transition and
the operator needs to correct state without touching the ORM.

    python manage.py onboarding_complete <student_id_or_email> <step_key> \\
        [--term CODE] [--status completed|not_applicable] [--message "…"]
"""
from django.core.management.base import BaseCommand, CommandError

from ...api import complete_step, mark_not_applicable
from ...models import StudentOnboarding, StudentOnboardingStep
from .._lookup import resolve_student


class Command(BaseCommand):
    help = 'Mark a step completed or not_applicable for a student.'

    def add_arguments(self, parser):
        parser.add_argument('student', help='Student UUID or email.')
        parser.add_argument('step_key',
                            help='Step key (e.g. ferpa, classes, pay_tuition).')
        parser.add_argument('--term', default=None,
                            help='Term code (default: active term).')
        parser.add_argument('--status', default='completed',
                            choices=['completed', 'not_applicable'],
                            help='Target status (default: completed).')
        parser.add_argument('--message', default=None,
                            help='Optional message recorded on the step.')

    def handle(self, *args, **opts):
        student = resolve_student(opts['student'])
        term = self._resolve_term(opts['term'])

        step_exists = StudentOnboardingStep.objects.filter(
            onboarding__student=student,
            onboarding__term=term,
            key=opts['step_key'],
        ).exists()
        if not step_exists:
            raise CommandError(
                f'No step {opts["step_key"]!r} for student {student.pk} '
                f'in term {term.code}. '
                f'Run `onboarding_dispatch application_started '
                f'--student {student.pk}` first to seed.'
            )

        if opts['status'] == 'completed':
            complete_step(student, key=opts['step_key'],
                          message=opts['message'] or None, term=term)
        else:
            mark_not_applicable(student, key=opts['step_key'], term=term)

        self.stdout.write(self.style.SUCCESS(
            f'Marked {opts["step_key"]!r} as {opts["status"]} '
            f'for {student.pk} in term {term.code}.'
        ))

    @staticmethod
    def _resolve_term(code):
        from cis.models.term import Term
        from cis.utils import active_term
        if code:
            try:
                return Term.objects.get(code=code)
            except Term.DoesNotExist:
                raise CommandError(f'No term found with code {code!r}.')
        return active_term()
