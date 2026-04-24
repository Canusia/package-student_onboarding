"""Staff-facing DRF endpoints for the /ce/students/ onboarding summary tabs."""
import datetime

from django.db.models import Count, Q, Max, F, Prefetch
from django.utils import timezone
from rest_framework import viewsets, views
from rest_framework.response import Response

from cis.utils import CIS_user_only, active_term
from cis.models.highschool import HighSchool

from .models import (
    StudentOnboarding,
    StudentOnboardingStep,
    DailyOnboardingStats,
)
from .serializers import OnboardingByStudentSerializer


def _resolve_term(request):
    term_id = request.GET.get('term_id', '').strip()
    if term_id:
        from cis.models.term import Term
        try:
            return Term.objects.get(id=term_id)
        except Term.DoesNotExist:
            pass
    return active_term()


class OnboardingByStudentViewSet(viewsets.ReadOnlyModelViewSet):
    """Row per StudentOnboarding for the selected term, with progress."""
    serializer_class = OnboardingByStudentSerializer
    permission_classes = [CIS_user_only]

    def get_queryset(self):
        term = _resolve_term(self.request)
        qs = (
            StudentOnboarding.objects
            .select_related('student__user', 'student__highschool')
            .prefetch_related('steps')
        )
        if term:
            qs = qs.filter(term=term)

        highschool_id = self.request.GET.get('highschool_id', '').strip()
        if highschool_id:
            qs = qs.filter(student__highschool_id=highschool_id)

        status = self.request.GET.get('status', '').strip()
        if status == 'completed':
            qs = qs.filter(completed_on__isnull=False)
        elif status == 'pending':
            qs = qs.filter(completed_on__isnull=True)

        return qs


class OnboardingByHighSchoolView(views.APIView):
    """Aggregated counts by high school."""
    permission_classes = [CIS_user_only]

    def get(self, request):
        term = _resolve_term(request)
        if not term:
            return Response({'data': [], 'recordsTotal': 0, 'recordsFiltered': 0})

        rows = (
            StudentOnboarding.objects
            .filter(term=term)
            .values(
                'student__highschool_id',
                'student__highschool__name',
            )
            .annotate(
                total=Count('id'),
                completed=Count('id', filter=Q(completed_on__isnull=False)),
                pending=Count('id', filter=Q(completed_on__isnull=True)),
            )
            .order_by('student__highschool__name')
        )

        data = []
        for r in rows:
            total = r['total'] or 0
            completed = r['completed'] or 0
            percent = int(100 * completed / total) if total else 0
            data.append({
                'highschool_id': str(r['student__highschool_id']) if r['student__highschool_id'] else '',
                'highschool': r['student__highschool__name'] or '(none)',
                'total': total,
                'completed': completed,
                'pending': r['pending'] or 0,
                'percent': percent,
            })

        return Response({
            'data': data,
            'recordsTotal': len(data),
            'recordsFiltered': len(data),
        })


class OnboardingStalledView(views.APIView):
    """Students with no step progress for the last N days."""
    permission_classes = [CIS_user_only]

    def get(self, request):
        term = _resolve_term(request)
        if not term:
            return Response({'data': [], 'recordsTotal': 0, 'recordsFiltered': 0})

        try:
            days = int(request.GET.get('days', '7'))
        except ValueError:
            days = 7
        cutoff = timezone.now() - datetime.timedelta(days=days)

        qs = (
            StudentOnboarding.objects
            .filter(term=term, completed_on__isnull=True)
            .select_related('student__user', 'student__highschool')
            .annotate(latest_step_completion=Max('steps__completed_on'))
            .filter(
                Q(latest_step_completion__lt=cutoff) |
                Q(latest_step_completion__isnull=True, started_on__lt=cutoff)
            )
            .order_by('started_on')
        )

        data = [{
            'id': o.id,
            'student_id': str(o.student_id),
            'first_name': o.student.user.first_name,
            'last_name': o.student.user.last_name,
            'email': o.student.user.email,
            'highschool': o.student.highschool.name if o.student.highschool_id else '',
            'started_on': o.started_on,
            'last_step_completed_on': o.latest_step_completion,
            'last_notified_on': o.last_notified_on,
            'progress_percent': o.progress_percent,
            'pending_count': o.total_steps - o.completed_steps,
        } for o in qs]

        return Response({
            'data': data,
            'recordsTotal': len(data),
            'recordsFiltered': len(data),
        })


class OnboardingTimelineView(views.APIView):
    """Cumulative started/completed counts across dates (from DailyOnboardingStats)."""
    permission_classes = [CIS_user_only]

    def get(self, request):
        term = _resolve_term(request)
        if not term:
            return Response([])

        qs = DailyOnboardingStats.objects.filter(term=term, step_key='')

        highschool_id = request.GET.get('highschool_id', '').strip()
        if highschool_id:
            qs = qs.filter(highschool_id=highschool_id)
        else:
            qs = qs.filter(highschool__isnull=True)

        qs = qs.order_by('date').values('date', 'started_count', 'completed_count')
        return Response(list(qs))


class OnboardingFunnelView(views.APIView):
    """Per-step completion counts for the most recent aggregation date."""
    permission_classes = [CIS_user_only]

    def get(self, request):
        term = _resolve_term(request)
        if not term:
            return Response([])

        highschool_id = request.GET.get('highschool_id', '').strip()

        steps_qs = StudentOnboardingStep.objects.filter(onboarding__term=term)
        if highschool_id:
            steps_qs = steps_qs.filter(onboarding__student__highschool_id=highschool_id)

        rows = (
            steps_qs
            .values('key', 'label')
            .annotate(
                completed=Count('id', filter=Q(status='completed')),
                not_applicable=Count('id', filter=Q(status='not_applicable')),
                pending=Count('id', filter=Q(status='pending')),
            )
            .order_by('key')
        )
        return Response(list(rows))
