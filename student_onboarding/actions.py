"""Student-record action: preview this student's onboarding reminder.

Registers into the host project's `student_actions` registry
(`myce/component_registry/student.py`) so the item appears in the Actions
dropdown on the CE student detail page, alongside the actions cis registers
itself. `mou` is the precedent for a packaged app importing
`myce.component_registry`.

The handler mirrors cis's `download_student_pdf`: read the selected id from
`ids[]` and return an `open` outcome pointing at an existing view, rather
than rendering anything here.
"""
from django.http import JsonResponse
from django.urls import reverse

from myce.component_registry.student import student_actions


@student_actions.action(
    'email',
    label='Onboarding Reminder',
    icon='fas fa-envelope-open-text',
    scope=['detail'],
    slug='pending_onboarding_preview',
)
def pending_onboarding_preview(request):
    ids = request.POST.getlist('ids[]')
    if not ids:
        return JsonResponse({
            'outcome': 'alert',
            'status': 'error',
            'title': 'Error',
            'message': 'No record selected.',
        })
    return JsonResponse({
        'outcome': 'open',
        'url': reverse('student_onboarding_ce:pending_notification_detail',
                       args=[ids[0]]),
    })
