"""Outer proxy — re-exports the real module from the inner package."""
from student_onboarding.student_onboarding.settings.student_regis_pending import *  # noqa: F401,F403
from student_onboarding.student_onboarding.settings.student_regis_pending import (  # noqa: F401
    SettingForm,
    student_regis_pending,
)
