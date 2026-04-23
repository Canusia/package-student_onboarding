"""Manually dispatch an onboarding event for one or more students.

    python manage.py onboarding_dispatch <event_key> \\
        [--student <id|email>] [--all] [--dry-run] [--limit N] [--yes]
"""
from django.core.management.base import BaseCommand, CommandError

from ... import handlers
from ...signals import onboarding_event
from .._lookup import resolve_student
from cis.models.student import Student


class Command(BaseCommand):
    help = 'Dispatch an onboarding_event for one or more students.'

    def add_arguments(self, parser):
        parser.add_argument('event_key',
                            help='Event name (e.g. ferpa_completed).')
        parser.add_argument('--student', action='append', default=[],
                            help='Student UUID or email. Repeatable.')
        parser.add_argument('--all', action='store_true',
                            help='Dispatch for every Student row.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Cap total dispatches (paired with --all).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report targets, dispatch nothing.')
        parser.add_argument('--yes', action='store_true',
                            help='Skip confirmation prompt for --all.')
        parser.add_argument('--verbose-each', action='store_true',
                            help='Log every dispatch (default: summary only).')

    def handle(self, *args, **opts):
        event_key = opts['event_key']
        known = set(handlers.registered_events().keys())
        if not known:
            raise CommandError(
                'No handlers registered — onboarding bridge is not wired.'
            )
        if event_key not in known:
            raise CommandError(
                f'Unknown event {event_key!r}. '
                f'Known: {", ".join(sorted(known))}'
            )

        if opts['all'] and opts['student']:
            raise CommandError('Use --all or --student, not both.')
        if not (opts['all'] or opts['student']):
            raise CommandError('One of --all or --student is required.')

        students = self._resolve_targets(opts)
        n = students.count() if hasattr(students, 'count') else len(students)

        if opts['all'] and not opts['yes'] and not opts['dry_run']:
            prompt = f'Dispatch {event_key!r} to {n} students? [y/N] '
            if input(prompt).strip().lower() != 'y':
                self.stdout.write('Aborted.')
                return

        sent = 0
        for student in students:
            if opts['dry_run']:
                if opts['verbose_each']:
                    self.stdout.write(f'would dispatch {event_key} -> {student.pk}')
                sent += 1
                continue
            onboarding_event.send(
                sender=__name__, event=event_key, student=student,
            )
            if opts['verbose_each']:
                self.stdout.write(f'dispatched {event_key} -> {student.pk}')
            sent += 1

        prefix = '[dry-run] ' if opts['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Dispatched {event_key!r} to {sent} student(s).'
        ))

    def _resolve_targets(self, opts):
        if opts['student']:
            return [resolve_student(v) for v in opts['student']]
        qs = Student.objects.all().order_by('pk')
        if opts['limit']:
            qs = qs[:opts['limit']]
        return qs
