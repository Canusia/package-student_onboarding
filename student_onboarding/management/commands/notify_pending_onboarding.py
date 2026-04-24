"""
Notify students with incomplete onboarding for the active term.

Walks `StudentOnboarding` rows where `completed_on is null`, collects each
student's pending step labels, filters by the admin-configured
`missing_items` setting, rate-limits by `last_notified_on`, and sends an
email using the subject/body from the `student_regis_pending` setting.

Replaces the legacy `notify_students_signatures` command; the legacy one
remains as a thin shim so existing CronTab entries keep working.
"""
import datetime
import json
from collections import Counter

from django.conf import settings as dj_settings
from django.core.management.base import BaseCommand
from django.core.validators import validate_email
from django.template import Context, Template
from django.template.loader import get_template
from django.utils import timezone
from django.utils.safestring import mark_safe

from cis.signals.crontab import cron_task_started, cron_task_done
from cis.utils import active_term

from ...models import StudentOnboarding, StudentOnboardingStep


# Legacy `missing_items` values → step keys. Old saved settings used these
# tokens; new saves use step keys directly. Keep the map so existing DB rows
# continue to work.
LEGACY_MISSING_ITEM_ALIASES = {
    'unverified_students': 'verify_email',
    'not_registered': 'classes',
    'missing_ferpa': 'ferpa',
    'missing_student_agreement': 'student_agreement',
    'missing_faa': 'tuition_assistance',
}


class Command(BaseCommand):
    help = 'Notify students with pending onboarding steps for the active term.'

    def add_arguments(self, parser):
        parser.add_argument('-t', '--time', type=str, help='Scheduled run time')
        parser.add_argument('--dry-run', action='store_true',
                            help='Build plan but do not send emails.')
        parser.add_argument('--student', type=str, default=None,
                            help='Process only the student with this id.')

    def handle(self, *args, **opts):
        # Import here to avoid AppRegistryNotReady at import time.
        from student_onboarding.settings.student_regis_pending import student_regis_pending
        from mailer import send_html_mail

        scheduled_time = opts.get('time')
        if scheduled_time:
            cron_task_started.send(
                sender=self.__class__, task=self.__class__,
                scheduled_time=scheduled_time,
            )

        summary, detailed_log = self._run(
            student_regis_pending=student_regis_pending,
            send_html_mail=send_html_mail,
            dry_run=opts.get('dry_run', False),
            only_student=opts.get('student'),
        )

        self.stdout.write(summary)

        if scheduled_time:
            cron_task_done.send(
                sender=self.__class__, task=self.__class__,
                scheduled_time=scheduled_time,
                summary=summary,
                detailed_log=json.dumps(detailed_log, default=str),
            )

    def _run(self, *, student_regis_pending, send_html_mail, dry_run, only_student):
        email_settings = student_regis_pending.from_db()
        is_active = email_settings.get('is_active', 'No')

        if is_active == 'No':
            return 'Notification disabled (is_active=No). Skipped.', {}

        term = active_term()
        if term is None:
            return 'No active term. Skipped.', {}

        freq = int(email_settings.get('freq') or 3)
        cutoff = timezone.now() - datetime.timedelta(days=freq)
        allowed_step_keys = {
            LEGACY_MISSING_ITEM_ALIASES.get(item, item)
            for item in (email_settings.get('missing_items') or [])
        }

        notify_address = [
            a.strip() for a in (email_settings.get('notify_address') or '').split(',')
            if a.strip()
        ]
        subject = email_settings.get('pending_app_email_subject') or ''
        body_template = Template(email_settings.get('pending_app_email') or '')
        html_wrapper = get_template('cis/email.html')
        add_note = (email_settings.get('add_note') or 'No') == 'Yes'
        debug_mode = is_active == 'Debug' or getattr(dj_settings, 'DEBUG', False)

        qs = (
            StudentOnboarding.objects
            .filter(term=term, completed_on__isnull=True)
            .select_related('student__user')
        )
        if only_student:
            qs = qs.filter(student_id=only_student)

        log = {'sent': [], 'skipped_rate_limit': [], 'skipped_no_match': [], 'skipped_all_done': []}
        by_step = Counter()  # step_key -> count of notifications that included that step

        for onboarding in qs.iterator():
            student = onboarding.student

            pending = list(
                onboarding.steps
                .exclude(status__in=[
                    StudentOnboardingStep.STATUS_COMPLETED,
                    StudentOnboardingStep.STATUS_NOT_APPLICABLE,
                ])
                .order_by('order')
            )
            if not pending:
                log['skipped_all_done'].append(str(student.id))
                continue

            if allowed_step_keys:
                pending = [s for s in pending if s.key in allowed_step_keys]
                if not pending:
                    log['skipped_no_match'].append(str(student.id))
                    continue

            if onboarding.last_notified_on and onboarding.last_notified_on > cutoff:
                log['skipped_rate_limit'].append(str(student.id))
                continue

            issues = [s.label for s in pending]
            context = Context({
                'missing_items': mark_safe('<br>'.join(issues)),
                'student_first_name': student.user.first_name,
                'student_last_name': student.user.last_name,
            })
            text_body = body_template.render(context)
            html_body = html_wrapper.render({'message': text_body})

            if debug_mode:
                to = list(notify_address) or [getattr(dj_settings, 'DEFAULT_FROM_EMAIL', '')]
            else:
                to = []
                try:
                    validate_email(student.user.email)
                    to.append(student.user.email)
                except Exception:
                    pass
                parent_email = getattr(student, 'parent_email', '') or ''
                if parent_email:
                    try:
                        validate_email(parent_email)
                        to.append(parent_email)
                    except Exception:
                        pass
                if not to:
                    log['skipped_no_match'].append(str(student.id))
                    continue

            if not dry_run:
                send_html_mail(
                    subject,
                    text_body,
                    html_body,
                    getattr(dj_settings, 'DEFAULT_FROM_EMAIL', ''),
                    to,
                )
                onboarding.last_notified_on = timezone.now()
                onboarding.save(update_fields=['last_notified_on'])
                if add_note:
                    try:
                        student.add_note(None, 'Sent pending onboarding reminder - ' + ','.join(issues))
                    except Exception:
                        pass

            log['sent'].append({'student_id': str(student.id), 'issues': issues, 'to': to})
            for step in pending:
                by_step[step.key] += 1

        log['by_step'] = dict(by_step)

        from student_onboarding.step_registry import get as get_step
        def _label(key):
            step = get_step(key)
            return step.label if step else key
        breakdown_parts = [
            f'{count} {_label(key)}'
            for key, count in sorted(by_step.items(), key=lambda kv: -kv[1])
        ]
        breakdown = '; '.join(breakdown_parts) if breakdown_parts else '-'

        summary = (
            f"{'[dry-run] ' if dry_run else ''}"
            f"Sent {len(log['sent'])} | "
            f"rate-limited {len(log['skipped_rate_limit'])} | "
            f"no matching items {len(log['skipped_no_match'])} | "
            f"all done {len(log['skipped_all_done'])}\n"
            f"By step: {breakdown}"
        )
        return summary, log
