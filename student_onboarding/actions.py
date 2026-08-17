"""Student-record action: preview this student's onboarding reminder.

Registers into the host project's `student_actions` registry
(`myce/component_registry/student.py`) so the item appears in the Actions
dropdown on the CE student detail page, alongside the actions cis registers
itself. `mou` is the precedent for a packaged app importing
`myce.component_registry`.

The handler reads the selected id from `ids[]` and returns a `redirect`
outcome pointing at an existing view, rather than rendering anything here.
It deliberately does NOT use the `open` outcome that cis's
`download_student_pdf` uses: a PDF belongs in its own tab, but this is a page
staff read and come back from, so it navigates in place and carries a `back`
parameter the target renders as a Back button.
"""
from urllib.parse import urlencode

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
    target = reverse('student_onboarding_ce:pending_notification_detail',
                     args=[ids[0]])
    try:
        back = reverse('cis:student', args=[ids[0]])
    except Exception:
        # A deployment without the cis student page still gets the preview,
        # just without a Back button.
        back = ''

    return JsonResponse({
        'outcome': 'redirect',
        'url': f'{target}?{urlencode({"back": back})}' if back else target,
    })
