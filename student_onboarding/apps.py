from django.apps import AppConfig
from django.dispatch import receiver


class StudentOnboardingConfig(AppConfig):
    """Production config — used when this package is pip-installed as
    `student_onboarding` (top-level)."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'student_onboarding'

    def ready(self):
        from . import handlers
        from .signals import onboarding_event

        @receiver(onboarding_event, dispatch_uid='student_onboarding.bridge')
        def _bridge(sender, event, student, **kwargs):
            kwargs.pop('signal', None)
            handlers.dispatch(event, student=student, **kwargs)


class DevStudentOnboardingConfig(StudentOnboardingConfig):
    """Dev config — used when this submodule is checked out under
    `webapp/student_onboarding/`, making the inner package accessible
    as `student_onboarding.student_onboarding`."""
    name = 'student_onboarding.student_onboarding'
