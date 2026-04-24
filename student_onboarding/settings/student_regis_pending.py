"""
Settings form for the per-term "notify students with pending onboarding"
job. Moved from cis/settings/student_regis_pending.py. The tenant-theme
email wrapper (cis/templates/cis/email.html) still lives in cis; only the
form + setting-key logic is owned by this app.

The `Setting` DB key is unchanged so existing values keep loading.
"""
from django import forms
from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse_lazy

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit

from cis.validators import validate_html_short_code
from cis.models.crontab import CronTab
from cis.models.settings import Setting
from cis.validators import validate_cron, numeric, validate_email_list
from cis.utils import YES_NO_SELECT_OPTIONS


class SettingForm(forms.Form):

    STATUS_OPTIONS = [
        ('', 'Select'),
        ('Yes', 'Yes'),
        ('No', 'No'),
        ('Debug', 'Debug'),
    ]

    is_active = forms.ChoiceField(
        choices=STATUS_OPTIONS,
        label='Enabled',
        help_text='',
        widget=forms.Select(attrs={'class': 'col-md-4 col-sm-12'}))

    notify_address = forms.CharField(
        help_text='Comma separated list of (staff/testers) email addresses for debug mode, and also for notifying when roster status is changed',
        label="Notification List",
        validators=[validate_email_list]
    )

    # Choices are populated from the step registry in __init__ so new steps
    # become notifiable automatically without editing this form.
    missing_items = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        choices=[],
    )
    pending_app_email_subject = forms.CharField(
        max_length=200,
        help_text='',
        label="Subject")

    pending_app_email = forms.CharField(
        max_length=None,
        widget=forms.Textarea,
        validators=[validate_html_short_code],
        help_text='Email template must include {{missing_items}}. Can also include {{student_first_name}}, {{student_last_name}}. <a href="#" class="float-right" onClick="do_bulk_action(\'student_regis_pending\', \'pending_app_email\')" >See Preview</a>',
        label="Email")

    freq = forms.CharField(
        max_length=2,
        help_text='Frequency at which a student should be notified, 1 - 10',
        label='Send every # Days',
        validators=[numeric]
    )

    cron = forms.CharField(
        max_length=20,
        help_text='Min Hr Day Month WeekDay. Controls when the job is run',
        label="Cron Expression",
        validators=[validate_cron]
    )

    add_note = forms.ChoiceField(
        choices=YES_NO_SELECT_OPTIONS,
        label='Add Note to Student',
        help_text='Add note to student\'s record after a notification is sent. Note will include missing items'
    )

    def _to_python(self):
        cron, created = CronTab.objects.get_or_create(
            command='notify_students_signatures'
        )
        cron.cron = self.cleaned_data.get('cron')
        cron.save()

        return {
            'pending_app_email_subject': self.cleaned_data['pending_app_email_subject'],
            'is_active': self.cleaned_data['is_active'],
            'pending_app_email': self.cleaned_data['pending_app_email'],
            'freq': self.cleaned_data['freq'],
            'cron': self.cleaned_data['cron'],
            'add_note': self.cleaned_data.get('add_note'),
            'notify_address': self.cleaned_data.get('notify_address'),
            'missing_items': self.cleaned_data.get('missing_items'),
        }


class student_regis_pending(SettingForm):
    key = getattr(settings, 'CAMPUS_CODE_PREFIX') + "_student_regis_email"

    def __init__(self, request, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Populate missing_items choices from the step registry (filled by
        # host apps at ready() time).
        from student_onboarding.step_registry import notifiable_steps
        self.fields['missing_items'].choices = [
            (s.key, s.notify_label) for s in notifiable_steps()
        ]

        self.request = request
        self.helper = FormHelper()
        self.helper.attrs = {'target': '_blank'}
        self.helper.form_method = 'POST'
        self.helper.form_action = reverse_lazy(
            'setting:run_record', args=[request.GET.get('report_id')])
        self.helper.add_input(Submit('submit', 'Save Setting'))

    def preview(self, request, field_name):
        from django.shortcuts import render
        from django.utils.safestring import mark_safe
        from django.template import Context, Template

        email_settings = self.from_db()
        subject = email_settings.get('pending_app_email_subject')
        email = email_settings.get('pending_app_email')

        message = Template(email)
        context = Context({
            'student_first_name': "John",
            'student_last_name': "Smith",
            'student_email': "john@email.com",
            'missing_items': mark_safe("<br>".join({
                'Missing Student Agreement',
                'Did not apply for class(es)',
                'Missing Tuition Assistance application',
            })),
            'campus': "Campus Name",
        })

        text_body = message.render(context)

        return render(
            request,
            'cis/email.html',
            {'message': text_body}
        )

    @classmethod
    def from_db(cls):
        try:
            setting = Setting.objects.get(key=cls.key)
            return setting.value
        except Setting.DoesNotExist:
            return {}

    def install(self):
        defaults = {
            'pending_app_email_subject': "Change this in Settings -> Students -> Incomplete App Reminder Email",
            'is_active': 'No',
            'pending_app_email': "Change this in Settings -> Students -> Incomplete App Reminder Email",
            'freq': '3',
        }

        try:
            setting = Setting.objects.get(key=self.key)
        except Setting.DoesNotExist:
            setting = Setting()
            setting.key = self.key

        setting.value = defaults
        setting.save()

    def run_record(self):
        try:
            setting = Setting.objects.get(key=self.key)
        except Setting.DoesNotExist:
            setting = Setting()
            setting.key = self.key

        setting.value = self._to_python()
        setting.save()

        return JsonResponse({
            'message': 'Successfully saved settings',
            'status': 'success'})
