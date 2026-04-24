"""
Backfill StudentOnboarding records for existing students.

For each eligible student, dispatches APPLICATION_STARTED so the host's
registered handlers seed default steps (from the step registry), then
iterates the registry's `complete_when` predicates to pre-mark steps that
are already done in prior flows.

Run inside the django container:

    docker exec -w /app/webapp django_web_ewu \\
        python manage.py seed_onboarding [--dry-run] [--limit N] [--student <id>]
"""
from collections import Counter

from django.core.management.base import BaseCommand

from cis.models.student import Student
from cis.utils import active_term

from ...api import complete_step
from ...signals import onboarding_event
from ... import events as oe
from ...models import StudentOnboarding
from ...step_registry import all_steps


ELIGIBLE_STATUSES = ('pending', 'in_review', 'accepted')


class Command(BaseCommand):
    help = 'Seed StudentOnboarding rows for existing students for the active term.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would happen, write nothing.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N students.')
        parser.add_argument('--student', type=str, default=None,
                            help='Process a single student by id (UUID).')
        parser.add_argument('--all-statuses', action='store_true',
                            help='Include students regardless of application_status.')

    def handle(self, *args, **opts):
        term = active_term()
        if term is None:
            self.stderr.write(self.style.ERROR('No active term — aborting.'))
            return

        qs = Student.objects.all()
        if opts['student']:
            qs = qs.filter(pk=opts['student'])
        elif not opts['all_statuses']:
            qs = qs.filter(application_status__in=ELIGIBLE_STATUSES)

        # Skip students who already have onboarding for the active term.
        already = set(
            StudentOnboarding.objects
            .filter(term=term)
            .values_list('student_id', flat=True)
        )
        qs = qs.exclude(pk__in=already)

        if opts['limit']:
            qs = qs[:opts['limit']]

        total = qs.count() if hasattr(qs, 'count') else len(list(qs))
        self.stdout.write(f'Processing {total} student(s) for term {term.code}.')

        seeded = 0
        premarked = Counter()
        steps_with_predicate = [s for s in all_steps() if s.complete_when]

        for student in qs.iterator():
            if opts['dry_run']:
                seeded += 1
                continue

            onboarding_event.send(
                sender=__name__, event=oe.APPLICATION_STARTED, student=student,
            )
            seeded += 1

            for step in steps_with_predicate:
                try:
                    is_done = step.complete_when(student, term)
                except Exception:
                    is_done = False
                if is_done:
                    complete_step(
                        student, key=step.key,
                        message=step.complete_message or None,
                    )
                    premarked[step.key] += 1

        prefix = '[dry-run] ' if opts['dry_run'] else ''
        breakdown = ' | '.join(
            f'{key}:{count}' for key, count in sorted(premarked.items())
        ) or '-'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Seeded {seeded} | pre-marked {breakdown}'
        ))
