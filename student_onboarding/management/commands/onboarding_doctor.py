"""Health check for the onboarding plumbing.

Exits non-zero if anything looks broken so it can be wired into CI or
monitoring.

    python manage.py onboarding_doctor [--json] [--expect-event KEY ...]
"""
import json
import sys

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import Count

from ... import handlers, events
from ...models import StudentOnboarding, StudentOnboardingStep
from ...signals import onboarding_event


class Command(BaseCommand):
    help = 'Diagnose the onboarding app\'s wiring and data health.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true',
                            help='Emit a machine-readable JSON payload.')
        parser.add_argument('--expect-event', nargs='*', default=[],
                            help='Assert these event keys have handlers.')

    def handle(self, *args, **opts):
        checks = []
        checks.append(self._check_app_loaded())
        checks.append(self._check_bridge_attached())
        checks.append(self._check_registry_populated())
        checks.append(self._check_active_term())
        checks.append(self._check_tables_reachable())
        for key in opts['expect_event']:
            checks.append(self._check_event_has_handler(key))

        summary = self._summary_counts()

        if opts['json']:
            payload = {
                'ok': all(c['ok'] for c in checks),
                'checks': checks,
                'summary': summary,
            }
            self.stdout.write(json.dumps(payload, default=str, indent=2))
        else:
            self._print_text(checks, summary)

        if not all(c['ok'] for c in checks):
            sys.exit(1)

    # -------- individual checks --------

    def _check_app_loaded(self):
        try:
            cfg = apps.get_app_config('student_onboarding')
            return {'name': 'app_loaded', 'ok': True,
                    'detail': f'{cfg.__class__.__module__}.{cfg.__class__.__name__}'}
        except Exception as e:
            return {'name': 'app_loaded', 'ok': False, 'detail': str(e)}

    def _check_bridge_attached(self):
        n = len(onboarding_event.receivers)
        return {
            'name': 'bridge_attached',
            'ok': n > 0,
            'detail': f'{n} receiver(s) connected to onboarding_event',
        }

    def _check_registry_populated(self):
        reg = handlers.registered_events()
        return {
            'name': 'handlers_registered',
            'ok': bool(reg),
            'detail': {k: len(v) for k, v in reg.items()},
        }

    def _check_event_has_handler(self, key):
        n = len(handlers._handlers.get(key, []))
        return {
            'name': f'event:{key}',
            'ok': n > 0,
            'detail': f'{n} handler(s)',
        }

    def _check_active_term(self):
        try:
            from cis.utils import active_term
            term = active_term()
            if term is None:
                return {'name': 'active_term', 'ok': False, 'detail': 'None'}
            return {'name': 'active_term', 'ok': True, 'detail': term.code}
        except Exception as e:
            return {'name': 'active_term', 'ok': False, 'detail': str(e)}

    def _check_tables_reachable(self):
        try:
            StudentOnboarding.objects.exists()
            StudentOnboardingStep.objects.exists()
            return {'name': 'tables_reachable', 'ok': True, 'detail': '-'}
        except Exception as e:
            return {'name': 'tables_reachable', 'ok': False, 'detail': str(e)}

    def _summary_counts(self):
        try:
            ob_total = StudentOnboarding.objects.count()
            step_total = StudentOnboardingStep.objects.count()
            by_status = dict(
                StudentOnboardingStep.objects
                .values_list('status')
                .annotate(n=Count('id'))
            )
        except Exception as e:
            return {'error': str(e)}
        return {
            'onboardings': ob_total,
            'steps': step_total,
            'steps_by_status': by_status,
        }

    # -------- text output --------

    def _print_text(self, checks, summary):
        for c in checks:
            mark = (self.style.SUCCESS('✓') if c['ok']
                    else self.style.ERROR('✗'))
            self.stdout.write(f'  {mark} {c["name"]:<25} {c["detail"]}')
        self.stdout.write('')
        self.stdout.write(f'summary: {summary}')
