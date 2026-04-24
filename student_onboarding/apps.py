from django.apps import AppConfig
from django.db.models.signals import post_migrate

from . import handlers
from .signals import onboarding_event


def _bridge(sender, event, student, **kwargs):
    """Module-level bridge from the generic `onboarding_event` Django signal
    to the string-keyed handlers registry.

    Kept at module scope (not inside ready()) so the reference is held by
    the module for the life of the process. Django signals use weak refs
    by default, so a local function inside ready() gets garbage-collected
    shortly after ready() returns — and events then silently stop firing.
    """
    kwargs.pop('signal', None)
    handlers.dispatch(event, student=student, **kwargs)


def _register_cron(sender, **kwargs):
    try:
        from cis.models.crontab import CronTab
    except Exception:
        return
    CronTab.objects.get_or_create(
        command='aggregate_onboarding_stats',
        defaults={'cron': '0 2 * * *'},
    )


class StudentOnboardingConfig(AppConfig):
    """Production config — used when this package is pip-installed as
    `student_onboarding` (top-level)."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'student_onboarding'

    def ready(self):
        # weak=False is belt-and-suspenders alongside the module-level ref:
        # guarantees the receiver can never be GC'd out of the signal.
        onboarding_event.connect(
            _bridge,
            dispatch_uid='student_onboarding.bridge',
            weak=False,
        )

        post_migrate.connect(
            _register_cron,
            sender=self,
            dispatch_uid='student_onboarding.register_cron',
        )


class DevStudentOnboardingConfig(StudentOnboardingConfig):
    """Dev config — used when this submodule is checked out under
    `webapp/student_onboarding/`, making the inner package accessible
    as `student_onboarding.student_onboarding`."""
    name = 'student_onboarding.student_onboarding'
