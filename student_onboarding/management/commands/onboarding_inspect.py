"""Show onboarding state for a single student.

    python manage.py onboarding_inspect <student_id_or_email> [--term CODE] [--json]
"""
import json

from django.core.management.base import BaseCommand

from .._lookup import resolve_student
from ...models import StudentOnboarding


class Command(BaseCommand):
    help = 'Display onboarding rows and steps for a student.'

    def add_arguments(self, parser):
        parser.add_argument('student', help='Student UUID or email.')
        parser.add_argument('--term', default=None,
                            help='Limit output to a single term code.')
        parser.add_argument('--json', action='store_true',
                            help='Emit a machine-readable JSON payload.')

    def handle(self, *args, **opts):
        student = resolve_student(opts['student'])

        onboardings = (
            StudentOnboarding.objects
            .filter(student=student)
            .select_related('term')
            .prefetch_related('steps')
            .order_by('-term__code')
        )
        if opts['term']:
            onboardings = onboardings.filter(term__code=opts['term'])

        if opts['json']:
            self.stdout.write(json.dumps(self._as_json(student, onboardings),
                                         default=str, indent=2))
            return

        self._print_text(student, onboardings)

    # ------------------------------------------------------------------

    def _as_json(self, student, onboardings):
        return {
            'student_id': str(student.pk),
            'email': getattr(student.user, 'email', None),
            'application_status': getattr(student, 'application_status', None),
            'onboardings': [
                {
                    'term': ob.term.code,
                    'started_on': ob.started_on,
                    'completed_on': ob.completed_on,
                    'total_steps': ob.total_steps,
                    'completed_steps': ob.completed_steps,
                    'steps': [
                        {
                            'key': st.key,
                            'label': st.label,
                            'status': st.status,
                            'completed_on': st.completed_on,
                            'message': st.message,
                        }
                        for st in ob.steps.all().order_by('order')
                    ],
                }
                for ob in onboardings
            ],
        }

    def _print_text(self, student, onboardings):
        email = getattr(student.user, 'email', '-')
        status = getattr(student, 'application_status', '-')
        self.stdout.write(
            f'Student: {email} ({student.pk})  app_status={status}'
        )

        if not onboardings.exists():
            self.stdout.write(self.style.WARNING(
                '  (no onboarding rows — run onboarding_dispatch '
                'application_started --student <id>)'
            ))
            return

        balance = self._try_balance(student)
        for ob in onboardings:
            self.stdout.write('')
            self.stdout.write(
                f'Term {ob.term.code}   started={ob.started_on:%Y-%m-%d}   '
                f'completed_on={ob.completed_on or "-"}   '
                f'{ob.completed_steps}/{ob.total_steps} steps'
            )
            for st in ob.steps.all().order_by('order'):
                marker = {
                    'completed': self.style.SUCCESS('✓'),
                    'not_applicable': self.style.NOTICE('—'),
                }.get(st.status, self.style.WARNING('·'))
                when = (f'{st.completed_on:%Y-%m-%d %H:%M}'
                        if st.completed_on else '-')
                extra = ''
                if st.key == 'pay_tuition' and balance is not None:
                    extra = f'   balance=${balance:.2f}'
                self.stdout.write(
                    f'  {marker} {st.key:<14} {st.status:<14} {when}{extra}'
                )

    @staticmethod
    def _try_balance(student):
        try:
            return round(student.student_balance(), 2)
        except Exception:
            return None
