# Plan: Migrate `notify_students_signatures` into student_onboarding

## Context

`cis/management/commands/notify_students_signatures.py` (delegating to `StudentRegistration.notify_students` in `cis/models/section.py:2146-2418`) is a ~270-line legacy notifier that emails students about four ad-hoc "missing items": unverified email, no class application, no student agreement, no tuition-assistance (FAA) application. The logic duplicates what `student_onboarding` is already designed to track — per-term checklist steps with completion events — but today onboarding only covers `ferpa`, `classes`, and (conditionally) `verify_info`.

Goal: retire the legacy ad-hoc checks in favor of a single rule — **"if a student has any pending steps in their current-term onboarding, email them the list of pending step labels."** The notifier, its settings form, and the new steps all move into the `student_onboarding` submodule. The legacy command becomes a thin shim so existing `CronTab` entries keep working during the transition.

## Design Overview

- **Extend the onboarding default-step set** with `verify_email`, `student_agreement`, and (conditional) `tuition_assistance`, gated by the `missing_items` setting so admins can enable/disable per campus.
- **Wire completion events** from the three existing save points so those steps auto-complete.
- **New command** `notify_pending_onboarding` in the submodule walks `StudentOnboarding` records for active term where `completed_on is null`, emails each student their pending step labels, and records `last_notified_on` to rate-limit.
- **Move settings form** into the submodule under the same `Setting` DB key so admin UI keeps working with no data migration.
- **Old command becomes a shim** calling the new one; CronTab entry is left unchanged so scheduling isn't disturbed.

## New / Updated Steps

Extend `_seed_default_steps` in `cis/signals/onboarding.py` to conditionally add three new steps based on the `missing_items` setting and student state:

| key | label | Seeded when | Completed by event |
|---|---|---|---|
| `verify_email` | Verify your email | `not student.account_verified` | `EMAIL_VERIFIED` |
| `student_agreement` | Sign student agreement | always (per-term) | `STUDENT_AGREEMENT_SIGNED` |
| `tuition_assistance` | Submit tuition assistance application | `student.qualify_tuition_assistance` | `TUITION_ASSISTANCE_SUBMITTED` |

Existing steps (`ferpa`, `classes`, `verify_info`) are unchanged.

## Event Additions

In `student_onboarding/student_onboarding/events.py`, add three new event constants:

```python
EMAIL_VERIFIED              = 'email_verified'
STUDENT_AGREEMENT_SIGNED    = 'student_agreement_signed'
TUITION_ASSISTANCE_SUBMITTED = 'tuition_assistance_submitted'
```

In `cis/signals/onboarding.py`, add handler functions + register them in `register_handlers()`:

```python
def on_email_verified(student, **kwargs):
    complete_step(student, key='verify_email', message='Email verified.')

def on_student_agreement(student, **kwargs):
    complete_step(student, key='student_agreement', message='Student agreement signed.')

def on_tuition_assistance(student, **kwargs):
    complete_step(student, key='tuition_assistance', message='Tuition assistance application submitted.')
```

Dispatch `onboarding_event.send(...)` from the three completion points:

- `webapp/student/views/onboarding.py:165` — right after `student.save()` in `verify_email()`, dispatch `EMAIL_VERIFIED`.
- `webapp/cis/forms/student.py:1234` — right after `student_agreeement.save()` in `StudentAgreementForm.save()`, dispatch `STUDENT_AGREEMENT_SIGNED`.
- `webapp/student/views/billing.py:345` — right after `faa.save()` when status flips to `'Submitted'`, dispatch `TUITION_ASSISTANCE_SUBMITTED`.

## Data Model Change

Add to `StudentOnboarding` in `student_onboarding/student_onboarding/models.py`:

```python
last_notified_on = models.DateTimeField(null=True, blank=True)
```

Migration `0002_studentonboarding_last_notified_on.py` adds the field.

## Settings Form Move

Move `cis/settings/student_regis_pending.py` → `student_onboarding/student_onboarding/settings/student_regis_pending.py`.

- Keep **the same `Setting` DB key** (`{CAMPUS_CODE_PREFIX}_student_regis_email`) — no data migration needed.
- Keep class name `student_regis_pending` for back-compat with any registrars/imports.
- Update the `cis/apps.py:331` CONFIGURATORS registration to import from the new path.
- Drop the unused `homeschool_*` hidden fields while touching it (dead code — the only consumer was the commented-out parent consent path).
- Preview continues to render `cis/email.html` (the email HTML wrapper stays in cis; it's the tenant theme).

## New Management Command: `notify_pending_onboarding`

Location: `student_onboarding/student_onboarding/management/commands/notify_pending_onboarding.py`

Usage:
```
python manage.py notify_pending_onboarding [--time ISO8601] [--dry-run] [--student <id>]
```

Behavior:
1. Read settings via the settings class (`student_regis_pending.from_db()`).
2. Short-circuit if `is_active != 'Yes'` (and log to staff for `'Debug'`).
3. Query `StudentOnboarding.objects.filter(term=active_term(), completed_on__isnull=True).select_related('student__user')`.
4. For each onboarding:
   - Collect pending steps: `steps.exclude(status__in=[COMPLETED, NOT_APPLICABLE]).order_by('order')`.
   - Skip if `missing_items` setting doesn't request any of the present pending step keys — filters the notification pool to admin-configured items without creating new steps.
   - Skip if `last_notified_on` is within `freq` days (`timezone.now() - timedelta(days=freq)`).
   - Render email: subject/body from settings; `missing_items` context var = `'<br>'.join(step.label for step in pending)`; include `student_first_name`, `student_last_name`.
   - Debug mode: send to `notify_address` instead of student.
   - `send_html_mail` via existing wrapper (same as legacy).
   - Set `last_notified_on = timezone.now()` and save.
   - Optional `add_note` if setting enabled.
5. Return `(summary_str, detailed_log_dict)` — same shape as legacy so CronTab logging is unchanged.

Step-key ↔ `missing_items` setting mapping:

| `missing_items` value | Onboarding step key(s) |
|---|---|
| `unverified_students` | `verify_email` |
| `not_registered` | `classes` |
| `missing_student_agreement` | `student_agreement` |
| `missing_faa` | `tuition_assistance` |

## Legacy Command Shim

`cis/management/commands/notify_students_signatures.py` stays, but its body shrinks to:

```python
from django.core.management import call_command
# … preserve cron signal wiring …
call_command('notify_pending_onboarding', *args, **options)
```

This keeps the existing `CronTab.command='notify_students_signatures'` row working while we migrate. A follow-up PR can rename the CronTab entry once we're confident.

## Backfill

Existing `StudentOnboarding` records won't have the new steps. The `seed_onboarding` command already exists — extend it to (idempotently, via `api.add_step`) seed the three new steps for the active term based on current state, and auto-complete them where appropriate:

- `verify_email` → if `student.account_verified`, `add_step(..., status='completed')`.
- `student_agreement` → if `StudentAgreement.objects.filter(student=..., term=active_term).exists()`, same.
- `tuition_assistance` → if not `qualify_tuition_assistance`, skip; else if `has_faa()`, completed.

`add_step` is already idempotent, so re-running is safe.

## Critical Files

**New (in submodule):**
- `student_onboarding/student_onboarding/settings/__init__.py`
- `student_onboarding/student_onboarding/settings/student_regis_pending.py` (moved)
- `student_onboarding/student_onboarding/management/commands/notify_pending_onboarding.py`
- `student_onboarding/student_onboarding/migrations/0002_studentonboarding_last_notified_on.py`

**Modified (in submodule):**
- `student_onboarding/student_onboarding/models.py` — add `last_notified_on` field
- `student_onboarding/student_onboarding/events.py` — add 3 new event constants
- `student_onboarding/student_onboarding/management/commands/seed_onboarding.py` — backfill new steps

**Modified (in cis):**
- `cis/signals/onboarding.py` — add 3 new steps in `_seed_default_steps`, add 3 handler fns + register them
- `cis/apps.py:331` — update CONFIGURATORS import path for `student_regis_pending`
- `cis/forms/student.py` (around line 1234) — dispatch `STUDENT_AGREEMENT_SIGNED`
- `cis/management/commands/notify_students_signatures.py` — shrink to shim
- `cis/settings/student_regis_pending.py` — delete (moved)
- `cis/models/section.py:2146-2418` — delete `StudentRegistration.notify_students` (dead after shim switchover)

**Modified (in student portal):**
- `student/views/onboarding.py:165` — dispatch `EMAIL_VERIFIED`
- `student/views/billing.py:345` — dispatch `TUITION_ASSISTANCE_SUBMITTED`

## Reused Utilities

- `student_onboarding.api.add_step / complete_step / mark_not_applicable` — all step mutations.
- `student_onboarding.signals.onboarding_event` — event bus.
- `cis.utils.active_term` — term filter default.
- `cis.models.settings.Setting` — settings persistence (unchanged key).
- `cis/templates/cis/email.html` — email HTML wrapper (unchanged).
- `mailer.send_html_mail` — same transport legacy uses.
- `StudentRegistration.needs_notification` — may be copied/kept for freq math, but the new command will just compare `last_notified_on` directly against `timedelta(days=freq)`.

## Verification

1. **Migrations**
   ```
   docker exec django_web_ewu python webapp/manage.py makemigrations student_onboarding
   docker exec django_web_ewu python webapp/manage.py migrate
   ```

2. **Backfill existing students**
   ```
   docker exec django_web_ewu python webapp/manage.py seed_onboarding
   ```
   Spot-check in shell: a student known to have signed the agreement should have their `student_agreement` step in `completed` status.

3. **Unit tests** — add to `student_onboarding/student_onboarding/tests.py`:
   - Handler tests: each new event (`EMAIL_VERIFIED`, `STUDENT_AGREEMENT_SIGNED`, `TUITION_ASSISTANCE_SUBMITTED`) marks its step complete.
   - `notify_pending_onboarding`:
     - Student with pending steps and no prior notification → email sent, `last_notified_on` set.
     - Re-run within `freq` days → no email.
     - Re-run after `freq` days → email re-sent.
     - Student with all steps done → skipped.
     - Debug mode → email goes to `notify_address`.
     - `missing_items` filtering: disable `missing_faa` → tuition step doesn't trigger notification even if pending.
   ```
   docker exec django_web_ewu python webapp/manage.py test student_onboarding
   ```

4. **End-to-end dry run**
   ```
   docker exec django_web_ewu python webapp/manage.py notify_pending_onboarding --dry-run
   ```
   Review the returned summary/log.

5. **Legacy shim**
   ```
   docker exec django_web_ewu python webapp/manage.py notify_students_signatures --dry-run
   ```
   Confirm it calls through and produces identical output shape.

6. **Settings UI**
   - Visit the settings page (same URL as before) — confirm the form loads, preview renders, save persists the same DB key.

7. **Live triggers** — in three separate shells, simulate each completion and confirm the step flips in the DB:
   - `student.account_verified = True; student.save()` via verify_email flow.
   - Submit a `StudentAgreementForm` in the student portal.
   - Submit a `StudentTuitionAssistanceForm`.
