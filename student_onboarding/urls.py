"""URL config for CE staff onboarding-summary endpoints.

Mounted from myce/urls.py at `/ce/onboarding/`.
"""
from django.contrib.auth.decorators import user_passes_test
from django.urls import path, include
from rest_framework import routers

from cis.utils import user_has_cis_role

from .views import (
    OnboardingByStudentViewSet,
    OnboardingByHighSchoolView,
    OnboardingStalledView,
    OnboardingTimelineView,
    OnboardingFunnelView,
    pending_notifications,
    pending_notification_detail,
)

app_name = 'student_onboarding_ce'

router = routers.DefaultRouter()
router.register(r'by_student', OnboardingByStudentViewSet, basename='onboarding_by_student')

urlpatterns = [
    path('api/', include(router.urls)),
    path('api/by_highschool/', OnboardingByHighSchoolView.as_view(), name='by_highschool'),
    path('api/stalled/', OnboardingStalledView.as_view(), name='stalled'),
    path('api/timeline/', OnboardingTimelineView.as_view(), name='timeline'),
    path('api/funnel/', OnboardingFunnelView.as_view(), name='funnel'),
    path(
        'notifications/pending/',
        user_passes_test(user_has_cis_role, login_url='/')(pending_notifications),
        name='pending_notifications',
    ),
    path(
        'notifications/pending/<uuid:student_id>/',
        user_passes_test(user_has_cis_role, login_url='/')(
            pending_notification_detail),
        name='pending_notification_detail',
    ),
]
