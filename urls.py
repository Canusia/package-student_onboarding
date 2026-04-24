"""Outer proxy — re-exports URL patterns from the inner package."""
from student_onboarding.student_onboarding.urls import urlpatterns, app_name  # noqa: F401
