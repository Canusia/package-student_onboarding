"""DRF serializers for CE onboarding-summary endpoints."""
from rest_framework import serializers

from .models import StudentOnboarding


class OnboardingByStudentSerializer(serializers.ModelSerializer):
    student_id = serializers.CharField(source='student.id', read_only=True)
    first_name = serializers.CharField(source='student.user.first_name', read_only=True)
    last_name = serializers.CharField(source='student.user.last_name', read_only=True)
    email = serializers.CharField(source='student.user.email', read_only=True)
    highschool = serializers.CharField(source='student.highschool.name', read_only=True, default='')
    progress_percent = serializers.IntegerField(read_only=True)
    pending_steps = serializers.SerializerMethodField()
    last_step_completed_on = serializers.SerializerMethodField()

    class Meta:
        model = StudentOnboarding
        fields = [
            'id', 'student_id', 'first_name', 'last_name', 'email', 'highschool',
            'started_on', 'completed_on', 'last_notified_on',
            'total_steps', 'completed_steps', 'progress_percent',
            'pending_steps', 'last_step_completed_on',
        ]
        datatables_always_serialize = ['id', 'student_id', 'progress_percent']

    def get_pending_steps(self, obj):
        return [
            s.label for s in obj.steps.all()
            if s.status == 'pending'
        ]

    def get_last_step_completed_on(self, obj):
        latest = None
        for s in obj.steps.all():
            if s.completed_on and (latest is None or s.completed_on > latest):
                latest = s.completed_on
        return latest
