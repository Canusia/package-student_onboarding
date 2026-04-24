"""Outer proxy — re-exports from the inner package."""
from student_onboarding.student_onboarding.step_registry import (  # noqa: F401
    StepDefinition,
    register,
    all_steps,
    get,
    notifiable_steps,
)
