"""
Service: pending onboarding notifications
=========================================

Core logic for deciding which students get an "incomplete onboarding"
reminder and what that email says. Decoupled from the management command so
the same logic backs the cron job, the CE preview page, the per-student
detail page, and the "Send Now" button — a preview that used its own copy of
these rules would drift from what the cron actually does.

Public API
----------
build_plan(...) -> str | Plan
    Returns a skip-reason string (notifications off, or no active term), or a
    Plan holding one PlanRow per student considered, each classified with a
    `decision`. No side effects — safe for preview and reporting.

get_pending_notifications(...) -> str | list[PlanRow]
    Thin wrapper returning only the sendable rows. Mirrors the equivalent
    helper in instructor_app's incomplete_notifications service.

send_notifications(rows, *, config, term, ...) -> dict
    Sends the emails, stamps `last_notified_on`, fires each pending step's
    `notify_action`, and writes the student note when `add_note` is on.

Decision order matters: all-done, then no-matching-item, then rate-limited,
then no-valid-email. The management command derives its `detailed_log`
buckets from these, so reordering them changes the cron's log output.
"""
import datetime
from collections import Counter
from dataclasses import dataclass, field

from django.conf import settings as dj_settings
from django.core.validators import validate_email
from django.template import Context, Template
from django.template.loader import get_template
from django.utils import timezone
from django.utils.safestring import mark_safe

from cis.utils import active_term

from .models import StudentOnboarding, StudentOnboardingStep
from .step_registry import get as get_step


# Legacy `missing_items` values -> step keys. Old saved settings used these
# tokens; new saves use step keys directly. Keep the map so existing DB rows
# continue to work.
LEGACY_MISSING_ITEM_ALIASES = {
    'unverified_students': 'verify_email',
    'not_registered': 'classes',
    'missing_ferpa': 'ferpa',
    'missing_student_agreement': 'student_agreement',
    'missing_faa': 'tuition_assistance',
}

DECISION_SEND = 'send'
DECISION_RATE_LIMITED = 'rate_limited'
DECISION_NO_MATCH = 'no_match'
DECISION_ALL_DONE = 'all_done'
DECISION_NO_EMAIL = 'no_email'

DECISION_LABELS = {
    DECISION_SEND: 'Will be emailed at the next scheduled run',
    DECISION_RATE_LIMITED: 'Skipped - notified too recently',
    DECISION_NO_MATCH: 'Skipped - no pending step is selected for notification',
    DECISION_ALL_DONE: 'Skipped - every onboarding step is done',
    DECISION_NO_EMAIL: 'Skipped - no valid email address on file',
}

SKIP_INACTIVE = 'Pending-onboarding notifications are turned off (is_active = No).'
SKIP_NO_TERM = 'There is no active term, so no notifications would be sent.'


@dataclass
class PlanRow:
    onboarding: object
    student: object
    pending_steps: list = field(default_factory=list)
    missing_items: list = field(default_factory=list)
    to_email: list = field(default_factory=list)
    subject: str = ''
    body: str = ''
    html_body: str = ''
    decision: str = DECISION_SEND
    last_notified_on: object = None
    next_eligible_on: object = None

    @property
    def decision_label(self):
        return DECISION_LABELS.get(self.decision, self.decision)


@dataclass
class Plan:
    config: dict
    term: object
    debug_mode: bool
    rows: list = field(default_factory=list)

    @property
    def sendable(self):
        return [row for row in self.rows if row.decision == DECISION_SEND]

    def ids_with_decision(self, decision):
        return [str(row.student.id) for row in self.rows
                if row.decision == decision]

    def ids_with_any_decision(self, *decisions):
        """Ids for any of `decisions`, in queryset-iteration order.

        Not a concatenation of per-decision lists: the management command's
        `detailed_log` groups no-match and no-email students into one bucket,
        and the pre-refactor command appended them inline as it walked, so the
        interleaved order is part of the output contract.
        """
        wanted = set(decisions)
        return [str(row.student.id) for row in self.rows
                if row.decision in wanted]


def _load_config(settings_form=None):
    if settings_form is None:
        from student_onboarding.settings.student_regis_pending import (
            student_regis_pending as settings_form,
        )
    return settings_form.from_db()


def _allowed_step_keys(config):
    return {
        LEGACY_MISSING_ITEM_ALIASES.get(item, item)
        for item in (config.get('missing_items') or [])
    }


def build_plan(*, term=None, only_student=None, settings_form=None,
               ignore_rate_limit=False, force=False):
    """Classify every incomplete onboarding for `term`. No side effects.

    `force=True` bypasses the is_active=No short-circuit — used by the
    per-student detail page so staff can still preview and send by hand
    while the scheduled job is off.
    """
    config = _load_config(settings_form)
    is_active = config.get('is_active', 'No')

    if is_active == 'No' and not force:
        return SKIP_INACTIVE

    if term is None:
        term = active_term()
    if term is None:
        return SKIP_NO_TERM

    freq = int(config.get('freq') or 3)
    cutoff = timezone.now() - datetime.timedelta(days=freq)
    allowed_step_keys = _allowed_step_keys(config)
    notify_address = [
        a.strip() for a in (config.get('notify_address') or '').split(',')
        if a.strip()
    ]
    subject = config.get('pending_app_email_subject') or ''
    body_template = Template(config.get('pending_app_email') or '')
    html_wrapper = get_template('cis/email.html')
    debug_mode = is_active == 'Debug' or getattr(dj_settings, 'DEBUG', False)

    plan = Plan(config=config, term=term, debug_mode=debug_mode)

    qs = (
        StudentOnboarding.objects
        .filter(term=term, completed_on__isnull=True)
        .select_related('student__user', 'student__highschool')
    )
    if only_student:
        qs = qs.filter(student_id=only_student)

    for onboarding in qs.iterator():
        student = onboarding.student
        row = PlanRow(
            onboarding=onboarding,
            student=student,
            last_notified_on=onboarding.last_notified_on,
        )
        if onboarding.last_notified_on:
            row.next_eligible_on = (
                onboarding.last_notified_on + datetime.timedelta(days=freq)
            )
        plan.rows.append(row)

        pending = list(
            onboarding.steps
            .exclude(status__in=[
                StudentOnboardingStep.STATUS_COMPLETED,
                StudentOnboardingStep.STATUS_NOT_APPLICABLE,
            ])
            .order_by('order')
        )
        if not pending:
            # DECISION_ALL_DONE is a data-drift bucket, not the normal
            # completion path: api.complete_step / api.mark_not_applicable
            # both go through _transition_step -> recompute_completion,
            # which stamps completed_on the moment nothing is left pending
            # -- so a normally-finished onboarding is already excluded by
            # the completed_on__isnull=True filter above and never reaches
            # here. This branch instead catches rows where completed_on is
            # still NULL despite nothing pending: e.g. a step was completed
            # via a direct queryset write that bypassed recompute, a step
            # row was deleted after completion, or total_steps/completed_steps
            # drifted out of sync some other way.
            row.decision = DECISION_ALL_DONE
            continue

        if allowed_step_keys:
            pending = [s for s in pending if s.key in allowed_step_keys]
            if not pending:
                row.decision = DECISION_NO_MATCH
                continue

        row.pending_steps = pending
        row.missing_items = [s.label for s in pending]

        if (not ignore_rate_limit and onboarding.last_notified_on
                and onboarding.last_notified_on > cutoff):
            row.decision = DECISION_RATE_LIMITED
            continue

        context = Context({
            'missing_items': mark_safe('<br>'.join(row.missing_items)),
            'student_first_name': student.user.first_name,
            'student_last_name': student.user.last_name,
        })
        row.subject = subject
        row.body = body_template.render(context)
        row.html_body = html_wrapper.render({'message': row.body})

        if debug_mode:
            row.to_email = (list(notify_address)
                            or [getattr(dj_settings, 'DEFAULT_FROM_EMAIL', '')])
            continue

        to = []
        for address in (student.user.email,
                        getattr(student, 'parent_email', '') or ''):
            if not address:
                continue
            try:
                validate_email(address)
                to.append(address)
            except Exception:
                pass
        if not to:
            row.decision = DECISION_NO_EMAIL
            continue
        row.to_email = to

    return plan


def get_pending_notifications(**kwargs):
    """Skip-reason string, or the rows that would actually be emailed."""
    plan = build_plan(**kwargs)
    if isinstance(plan, str):
        return plan
    return plan.sendable


def send_notifications(rows, *, config, term, send_html_mail=None,
                       dry_run=False):
    """Send the emails for `rows` and record the side effects."""
    if send_html_mail is None:
        from mailer import send_html_mail

    allowed_step_keys = _allowed_step_keys(config)
    add_note = (config.get('add_note') or 'No') == 'Yes'
    from_email = getattr(dj_settings, 'DEFAULT_FROM_EMAIL', '')

    sent = []
    by_step = Counter()

    for row in rows:
        if not dry_run:
            send_html_mail(row.subject, row.body, row.html_body,
                           from_email, row.to_email)
            row.onboarding.last_notified_on = timezone.now()
            row.onboarding.save(update_fields=['last_notified_on'])

            for step in row.pending_steps:
                # notify_action requires the step to be explicitly selected
                # in missing_items — never fire on the empty-list default.
                if step.key not in allowed_step_keys:
                    continue
                step_def = get_step(step.key)
                if step_def and step_def.notify_action:
                    try:
                        step_def.notify_action(row.student, term)
                    except Exception:
                        pass

            if add_note:
                try:
                    row.student.add_note(
                        None,
                        'Sent pending onboarding reminder - '
                        + ','.join(row.missing_items),
                    )
                except Exception:
                    pass

        sent.append({
            'student_id': str(row.student.id),
            'issues': row.missing_items,
            'to': row.to_email,
        })
        for step in row.pending_steps:
            by_step[step.key] += 1

    return {'sent': sent, 'by_step': dict(by_step)}
