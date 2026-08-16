"""
Notify students with incomplete onboarding for the active term.

The walk/filter/send logic now lives in `services.py`, shared with the CE
preview pages; this command is the cron entry point around it.

Replaces the legacy `notify_students_signatures` command; the legacy one
remains as a thin shim so existing CronTab entries keep working.
"""
import json

from django.core.management.base import BaseCommand

from cis.signals.crontab import cron_task_started, cron_task_done
from cis.utils import active_term

from ... import services
from ...step_registry import get as get_step


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
        plan = services.build_plan(
            term=active_term(),
            only_student=only_student,
            settings_form=student_regis_pending,
        )
        if isinstance(plan, str):
            if plan == services.SKIP_INACTIVE:
                return 'Notification disabled (is_active=No). Skipped.', {}
            return 'No active term. Skipped.', {}

        result = services.send_notifications(
            plan.sendable,
            config=plan.config,
            term=plan.term,
            send_html_mail=send_html_mail,
            dry_run=dry_run,
        )

        log = {
            'sent': result['sent'],
            'skipped_rate_limit': plan.ids_with_decision(
                services.DECISION_RATE_LIMITED),
            # A student with no valid email address has always been logged
            # under `skipped_no_match`; keep that grouping.
            'skipped_no_match': (
                plan.ids_with_decision(services.DECISION_NO_MATCH)
                + plan.ids_with_decision(services.DECISION_NO_EMAIL)
            ),
            'skipped_all_done': plan.ids_with_decision(
                services.DECISION_ALL_DONE),
            'by_step': result['by_step'],
        }

        def _label(key):
            step = get_step(key)
            return step.label if step else key

        breakdown_parts = [
            f'{count} {_label(key)}'
            for key, count in sorted(result['by_step'].items(),
                                     key=lambda kv: -kv[1])
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
