"""Onboarding Pending Items report: students with an unfinished onboarding
for a term who still have one of the chosen steps pending. CSV export, and
usable as an announcements (bulk mailer) datasource.

Registered from REPORTS in apps.py; the report package imports
`<app>.reports.onboarding_pending_items.onboarding_pending_items` and calls
run() in a background task, so the user scope is re-derived from
task.created_by rather than a request.

Unlike the Onboarding Reminders page, this does not go through the reminder
planner: it lists everyone with a pending step, whether or not reminders are
on or the student was emailed recently.
"""
import csv
import datetime
import io

from django import forms
from django.core.files.base import ContentFile
from django.db.models import Prefetch
from django.urls import reverse_lazy

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit

from cis.models.highschool import HighSchool
from cis.models.student import Student
from cis.models.term import Term
from cis.utils import active_term, user_has_cis_role

from .. import step_registry
from ..models import StudentOnboarding, StudentOnboardingStep

HEADERS = ['First Name', 'Last Name', 'Email', 'Student ID', 'High School',
           'Term', 'Pending Items', 'Steps Done', 'Last Reminder']


def _list(values):
    """Task / datasource data is dict(request.POST): every value is a list."""
    if values is None or isinstance(values, (str, int)):
        values = [values]
    return [str(getattr(v, 'pk', v)) for v in values if v not in (None, '')]


def _csv_cell(value):
    """A leading =, +, - or @ is quoted with ' so a spreadsheet never runs it."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@') else text


def _highschool_choices():
    try:  # cis >= v0.1.34a: the current campus's active schools
        from cis.highschool_scope import picker_queryset
        return picker_queryset()
    except ImportError:
        return HighSchool.objects.filter(status__iexact='active').order_by('name')


def _step_choices():
    """Registered steps plus any key already stored (a step a tenant has since
    dropped from the registry still shows up by its stored label)."""
    choices = {s.key: s.label for s in step_registry.all_steps()}
    for key, label in (StudentOnboardingStep.objects
                       .values_list('key', 'label').distinct().order_by('key')):
        choices.setdefault(key, label or key)
    return list(choices.items())


def scoped_onboardings(user, term_id, step_keys=None, highschool_ids=None):
    """pending_onboardings() limited to the students ``user`` may see: none
    for a non-CE user, campus-scoped for CE staff, all for superusers."""
    if user is None or not user_has_cis_role(user):
        return []
    scope = None
    try:
        from cis.campus_gate import scope_students_by_campus
    except ImportError:
        pass
    else:
        scope = scope_students_by_campus(Student.objects.all(), user)
    return pending_onboardings(term_id, step_keys, highschool_ids, students=scope)


def pending_onboardings(term_id, step_keys=None, highschool_ids=None, students=None):
    """Unfinished onboardings for the term with a pending step in ``step_keys``
    (any pending step when empty), optionally within the ``students`` queryset.

    Each record gets ``pending_items`` (labels of all its pending steps) and
    ``pending_items_text``.
    """
    if not term_id:
        return []

    pending = StudentOnboardingStep.objects.filter(status='pending')
    if step_keys:
        pending = pending.filter(key__in=step_keys)

    qs = (StudentOnboarding.objects
          .filter(term_id=term_id, completed_on__isnull=True,
                  pk__in=pending.values('onboarding_id'))
          .select_related('student__user', 'student__highschool', 'term')
          .prefetch_related(Prefetch(
              'steps',
              queryset=StudentOnboardingStep.objects.filter(status='pending')
              .order_by('order', 'id'),
              to_attr='pending_steps'))
          .order_by('student__user__last_name', 'student__user__first_name'))
    if highschool_ids:
        qs = qs.filter(student__highschool_id__in=highschool_ids)

    if students is not None:
        qs = qs.filter(student__in=students)

    records = list(qs)
    for record in records:
        record.pending_items = [s.label or s.key for s in record.pending_steps]
        record.pending_items_text = '; '.join(record.pending_items)
    return records


class onboarding_pending_items(forms.Form):

    # Announcements datasource contract (announcement ReportDataSource).
    use_as_datasource = True
    datasource_descriptor = ('Students with an unfinished onboarding who still '
                             'have one of the selected items pending.')
    email_column = 'email'
    name_columns = ['FirstName', 'LastName']
    datasource_fields = {
        'FirstName': 'student.user.first_name',
        'LastName': 'student.user.last_name',
        'email': 'student.user.email',
        'HighSchool': 'student.highschool.name',
        'Term': 'term',
        'PendingItems': 'pending_items_text',
    }

    term = forms.ModelChoiceField(queryset=None, label='Term')
    pending_items = forms.MultipleChoiceField(
        choices=(), required=False, label='Pending Item(s)',
        widget=forms.CheckboxSelectMultiple,
        help_text='Students with any of these still pending. Leave all '
                  'unticked for any pending item.')
    highschools = forms.ModelMultipleChoiceField(
        queryset=None, required=False, label='High School(s)',
        help_text='Leave empty for all high schools.')

    request = None

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.helper = FormHelper()
        self.helper.attrs = {'target': '_blank'}
        self.helper.form_method = 'POST'
        self.helper.add_input(Submit('submit', 'Generate Export'))
        if request:
            self.helper.form_action = reverse_lazy(
                'report:run_report', args=[request.GET.get('report_id')])

        self.fields['term'].queryset = Term.objects.all().order_by('-code')
        current = active_term()
        if current is not None:
            self.fields['term'].initial = current.pk
        self.fields['pending_items'].choices = _step_choices()
        self.fields['highschools'].queryset = _highschool_choices()

    # -- data ---------------------------------------------------------------

    @staticmethod
    def _filters(data):
        term_ids = _list(data.get('term'))
        return (term_ids[0] if term_ids else None,
                _list(data.get('pending_items')), _list(data.get('highschools')))

    def records(self, user, data):
        return scoped_onboardings(user, *self._filters(data))

    def _user(self):
        return getattr(self.request, 'user', None)

    # -- announcements datasource -------------------------------------------

    def recipient_columns(self):
        return {token: token for token in self.datasource_fields}

    def _row(self, record):
        user, school = record.student.user, record.student.highschool
        return {
            'FirstName': user.first_name,
            'LastName': user.last_name,
            'email': [user.email],
            'HighSchool': school.name if school else '',
            'Term': str(record.term),
            'PendingItems': record.pending_items_text,
        }

    def get_recipients(self, data, user=None):
        """One row per student with an email. The bulk mailer has no request
        here, so pass ``user`` to scope; without one, CE-wide (the handoff
        itself is CE-only)."""
        user = user or self._user()
        records = (self.records(user, data) if user is not None
                   else pending_onboardings(*self._filters(data)))
        rows, seen = [], set()
        for record in records:
            email = (record.student.user.email or '').strip()
            if not email or email.lower() in seen:
                continue
            seen.add(email.lower())
            rows.append(self._row(record))
        return rows

    def sample_row(self):
        sample = {token: '{' + token + '}' for token in self.datasource_fields}
        sample['email'] = 'sample@example.com'
        sample['PendingItems'] = 'FERPA; Register for Classes'
        return sample

    # -- export -------------------------------------------------------------

    def run(self, task, data):
        from cis.backends.storage_backend import PrivateMediaStorage

        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        for record in self.records(task.created_by, data):
            user, school = record.student.user, record.student.highschool
            writer.writerow([_csv_cell(v) for v in (
                user.first_name, user.last_name, user.email,
                getattr(user, 'psid', ''),
                school.name if school else '',
                record.term,
                record.pending_items_text,
                f'{record.completed_steps} of {record.total_steps}',
                record.last_notified_on.strftime('%m/%d/%Y')
                if record.last_notified_on else '',
            )])

        file_name = ('onboarding_pending_items_'
                     + datetime.datetime.now().strftime('%m-%d-%Y_%H%M%S') + '.csv')
        storage = PrivateMediaStorage()
        path = storage.save(f'reports/{task.id}/{file_name}',
                            ContentFile(stream.getvalue().encode('utf-8')))
        return storage.url(path)
