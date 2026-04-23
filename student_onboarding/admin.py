from django.contrib import admin

from .models import StudentOnboarding, StudentOnboardingStep


class StudentOnboardingStepInline(admin.TabularInline):
    model = StudentOnboardingStep
    extra = 0
    fields = ('order', 'key', 'label', 'status', 'completed_on', 'url_name', 'message')
    readonly_fields = ('completed_on',)
    ordering = ('order', 'id')


@admin.register(StudentOnboarding)
class StudentOnboardingAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'student', 'term', 'completed_steps', 'total_steps',
        'progress_percent', 'started_on', 'completed_on',
    )
    list_filter = ('term', 'completed_on')
    search_fields = (
        'student__user__email',
        'student__user__first_name',
        'student__user__last_name',
        'student__user__psid',
    )
    raw_id_fields = ('student',)
    readonly_fields = ('started_on', 'completed_on', 'progress_percent')
    inlines = [StudentOnboardingStepInline]
    ordering = ('-started_on',)


@admin.register(StudentOnboardingStep)
class StudentOnboardingStepAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'onboarding', 'key', 'label', 'status', 'order', 'completed_on',
    )
    list_filter = ('status', 'key')
    search_fields = (
        'key', 'label',
        'onboarding__student__user__email',
        'onboarding__student__user__psid',
    )
    raw_id_fields = ('onboarding',)
    readonly_fields = ('completed_on',)
    ordering = ('-onboarding__started_on', 'order')
