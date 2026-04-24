# student_onboarding

Per-term student onboarding checklist for the MyCE student portal and CE staff console.

Tracks the steps a student must complete for the active term (FERPA, class application, email verification, student agreement, tuition-assistance, returning-student profile review, plus anything else host apps register), surfaces them through a sidebar progress card for students, a set of reporting tabs for staff on `/ce/students/`, and a notification job that emails students about items still pending.

Distributed as a Django app + pip-installable package using the same submodule pattern as `myce_invoice`. See `CLAUDE.md` for conventions and gotchas.

## Install

The package is dual-mode: pip-installed in production, git submodule in dev. Consumer code (`from student_onboarding.api import add_step`, `{% load student_onboarding %}`) is identical in both modes thanks to outer-level proxy modules.

### Step 1 — Get the code

**Production (pip):**

```
# requirements.txt
git+https://github.com/Canusia/package-student_onboarding.git@v0.0.2
```

**Dev (git submodule):**

```bash
cd <host-repo-root>
git submodule add https://github.com/Canusia/package-student_onboarding.git webapp/student_onboarding
git submodule update --init --recursive
```

### Step 2 — Wire into Django settings

```python
# webapp/myce/settings.py
import importlib.util

INSTALLED_APPS += [
    'student_onboarding.student_onboarding.apps.DevStudentOnboardingConfig'
    if importlib.util.find_spec('student_onboarding.student_onboarding')
    else 'student_onboarding.apps.StudentOnboardingConfig',
]

TEMPLATES[0]['OPTIONS']['context_processors'].append(
    'student_onboarding.context_processors.onboarding_progress'
)
```

### Step 3 — Mount the CE staff URLs

The new staff-facing DRF endpoints (by_student / by_highschool / stalled / timeline / funnel) live inside this package. Mount once in the host's URL conf:

```python
# webapp/myce/urls.py
urlpatterns += [
    path('ce/onboarding/', include('student_onboarding.urls')),
]
```

### Step 4 — Run migrations

```bash
python manage.py migrate student_onboarding
```

Creates `student_onboarding_studentonboarding`, `student_onboarding_studentonboardingstep`, and `student_onboarding_dailyonboardingstats`. The `post_migrate` hook also registers a `CronTab` entry for the daily aggregate job (`0 2 * * *`), idempotently.

### Step 5 — Declare your steps in the host app

All domain knowledge lives in the host app — what steps exist, when to seed them, when to consider them already-done for a returning student, and which ones admins should be able to notify on. Host apps populate the step registry once, from `AppConfig.ready()`.

```python
# webapp/cis/signals/onboarding.py
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from student_onboarding import handlers, events
from student_onboarding.api import complete_step, get_or_create_for_current_term
from student_onboarding.step_registry import StepDefinition, register, all_steps
from student_onboarding.signals import onboarding_event

from cis.models.student import Student
from cis.utils import active_term


# --- predicates (optional per step) ---

def _unverified(student):
    return not getattr(student, 'account_verified', True)

def _email_is_verified(student, term):
    return bool(getattr(student, 'account_verified', False))

def _ferpa_is_done(student, term):
    completed_for = (getattr(student, 'meta', {}) or {}).get('ferpa_completed_for') or []
    return term is not None and term.code in completed_for


# --- step definitions ---

STEPS = [
    StepDefinition(
        key='verify_email',
        label='Verify your email',
        order=5,
        message='Verify the email address on your application.',
        seeded_when=_unverified,            # only seed while the student is unverified
        complete_when=_email_is_verified,   # backfill command uses this
        complete_message='Email verified.',
        notify_label='Notify students who have not verified their email',  # admin-facing
    ),
    StepDefinition(
        key='ferpa',
        label='Complete FERPA release',
        order=10,
        url_name='student:ferpa',
        message='Review and sign your FERPA release for the term.',
        complete_when=_ferpa_is_done,
        complete_message='FERPA release signed.',
        notify_label='Notify students who have not completed FERPA release',
    ),
    # ...add more for classes, student_agreement, tuition_assistance, etc.
]


EVENT_TO_STEP_KEY = {
    events.FERPA_COMPLETED: 'ferpa',
    events.EMAIL_VERIFIED: 'verify_email',
    # ...one entry per step you want auto-completed from an event
}


def _seed_default_steps(student):
    from student_onboarding.api import add_step
    for step in all_steps():
        if step.seeded_when and not step.seeded_when(student):
            continue
        add_step(student, key=step.key, label=step.label,
                 url_name=step.url_name, order=step.order, message=step.message)


def register_handlers():
    # Populate the registry once at startup.
    for step in STEPS:
        register(step)

    # Bridge events to completion handlers.
    handlers.register(events.APPLICATION_STARTED, lambda s, **_: _seed_default_steps(s))
    for event_name, step_key in EVENT_TO_STEP_KEY.items():
        step_def = next(s for s in STEPS if s.key == step_key)
        msg = step_def.complete_message
        handlers.register(
            event_name,
            lambda s, _key=step_key, _msg=msg, **_: complete_step(s, key=_key, message=_msg),
        )


@receiver(user_logged_in, dispatch_uid='cis.onboarding.reseed_on_term_rollover')
def reseed_on_term_rollover(sender, request, user, **kwargs):
    student = Student.objects.filter(user=user).first()
    if student is None:
        return
    onboarding = get_or_create_for_current_term(student)
    if not onboarding.steps.exists():
        _seed_default_steps(student)
```

Call `register_handlers()` from your CIS app's `AppConfig.ready()`:

```python
# webapp/cis/apps.py
class CisConfig(AppConfig):
    name = 'cis'
    def ready(self):
        from cis.signals import onboarding as onboarding_signals
        onboarding_signals.register_handlers()
```

> **Why a registry?** `_seed_default_steps`, the notification settings form, the backfill command, and the reporting funnel all need the same list of steps. Keeping them in one registry (not scattered across `if` branches) means adding a new step — or flipping `notify_label` on an existing one — takes effect everywhere automatically.

### Step 6 — Dispatch events from your views

Every place where a step-completing action happens, send the matching event:

```python
from student_onboarding.signals import onboarding_event
from student_onboarding import events as onboarding_events

# New-student signup complete
onboarding_event.send(sender=__name__, event=onboarding_events.APPLICATION_STARTED, student=student)

# Email verified
onboarding_event.send(sender=__name__, event=onboarding_events.EMAIL_VERIFIED, student=student)

# FERPA / classes / profile / agreement / TA
onboarding_event.send(sender=__name__, event=onboarding_events.FERPA_COMPLETED, student=student)
onboarding_event.send(sender=__name__, event=onboarding_events.CLASSES_APPLIED, student=student)
onboarding_event.send(sender=__name__, event=onboarding_events.PROFILE_VERIFIED, student=student)
onboarding_event.send(sender=__name__, event=onboarding_events.STUDENT_AGREEMENT_SIGNED, student=student)
onboarding_event.send(sender=__name__, event=onboarding_events.TUITION_ASSISTANCE_SUBMITTED, student=student)
```

In the MyCE host repo these fire from `student/views/onboarding.py:verify_email`, `student/views/billing.py` (TA), `cis/forms/student.py` (agreement), and the matching FERPA/class/profile save paths.

### Step 7 — Add the student-facing visual partials

These live in the host's student portal app (`webapp/student/templates/student/partials/`) so each portal can restyle. Reference copies ship with the MyCE host repo:

- `_term_progress.html` — sidebar card with progress bar + per-step links. Include in your dashboard sidebar:

  ```django
  {% include "student/partials/_term_progress.html" %}
  ```

- `_term_step_nav.html` — horizontal chevron stepper. Each task view sets `current_task` in render context:

  ```django
  {% include "student/partials/_term_step_nav.html" %}
  ```

Both iterate `onboarding.steps.all`, so new registered steps appear with no template edits.

### Step 8 — Add the CE staff tabs to `cis/students/index.html`

The staff tabs are shipped as partials inside this package at `templates/student_onboarding/ce/`. The host page just `{% include %}`s them. In MyCE this means two small edits to `webapp/cis/templates/cis/students/index.html`:

1. **Inject the new tab buttons** alongside the existing nav-tabs:

   ```django
   <ul class="nav nav-tabs">
       <li class="nav-item">
           <a class="nav-link active" data-toggle="tab" href="#all">All {{page_title}}</a>
       </li>
       {% include "student_onboarding/ce/_tabs_nav.html" %}
   </ul>
   ```

2. **Inject the tab-pane bodies** inside `<div class="tab-content">`, after the existing `#all` pane:

   ```django
   <div class="tab-content">
       <div class="tab-pane active" id="all">…existing…</div>

       {% include "student_onboarding/ce/_tab_by_student.html" %}
       {% include "student_onboarding/ce/_tab_by_highschool.html" %}
       {% include "student_onboarding/ce/_tab_timeline.html" %}
       {% include "student_onboarding/ce/_tab_stalled.html" %}
   </div>
   ```

The `by_student` tab ships with a Chart.js funnel above its datatable, CSV / Print export buttons, bulk "Send Verification Link" / "Get Verification Link" actions (reusing whatever bulk-action slugs the host has registered for students), and a term selector that drives all five tabs.

3. **Pass the timeline bounds and active term** from the view that renders this template:

   ```python
   # webapp/cis/views/student.py
   from django.conf import settings
   from cis.models.settings import Setting
   from cis.utils import active_term

   def index(request):
       key = settings.CAMPUS_CODE_PREFIX + '_cis_registrations'
       active = active_term()
       return render(request, 'cis/students/index.html', {
           ...,
           'terms': Term.objects.all().order_by('-code'),
           'onboarding_active_term_id': str(active.id) if active else '',
           'onboarding_timeline_start': Setting.get_value(key, 'starting_date'),
           'onboarding_timeline_end':   Setting.get_value(key, 'tuition_pay_end_date'),
       })
   ```

### Step 9 — Register the notification settings configurator

The `student_regis_pending` settings form is shipped inside this package (`settings/student_regis_pending.py`). Register it in the host app's `CONFIGURATORS` so the setting shows up in `/ce/settings/`:

```python
# webapp/cis/apps.py
class CisConfig(AppConfig):
    CONFIGURATORS = [
        ...,
        {
            'name': 'student_regis_pending',
            'title': 'Incomplete Student Onboarding Notification',
            'description': '-',
            'categories': ['1'],  # Students
        },
    ]
```

A back-compat shim at `webapp/cis/settings/student_regis_pending.py` re-exports the real class so existing lookups (`from cis.settings.student_regis_pending import student_regis_pending`) keep working:

```python
from student_onboarding.settings.student_regis_pending import (
    SettingForm, student_regis_pending,
)
```

Then run:

```bash
docker exec -w /app/webapp django_web_ewu python manage.py register_settings
```

The form's `missing_items` checkboxes are built dynamically from every `StepDefinition` whose `notify_label` is non-empty — add a new step with a `notify_label` and the checkbox appears next time the settings page loads.

### Verify

```bash
docker exec -w /app/webapp django_web_ewu python manage.py check
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```

Browse to `/student/` (progress card in sidebar) and `/ce/students/` (four new tabs).

## Management commands

All under `python manage.py`. Wrap with `docker exec -w /app/webapp <django_container>` as appropriate.

| Command | What it does |
|---|---|
| `seed_onboarding [--dry-run] [--limit N] [--student ID]` | Backfill onboarding rows for existing students. Iterates every `StepDefinition.complete_when` to pre-mark steps the student already finished in prior flows. Safe to re-run. |
| `notify_pending_onboarding [--time ISO] [--dry-run] [--student ID]` | Walks `StudentOnboarding` rows for the active term where `completed_on IS NULL`, emails each student their pending step labels, rate-limits via `last_notified_on` + the configured `freq` days. Respects the `missing_items` admin setting — steps whose key isn't selected are ignored. Returns a per-step summary. Legacy name `notify_students_signatures` is a thin shim that forwards here, so the existing CronTab entry keeps working. |
| `aggregate_onboarding_stats [--date YYYY-MM-DD] [--term ID] [--backfill-from YYYY-MM-DD]` | Idempotent daily rollup into `DailyOnboardingStats`. Writes all-HS, per-HS, and per-step rows so the timeline / by-HS / funnel endpoints are cheap. Registered as `CronTab` at `0 2 * * *` via `post_migrate`. |
| `onboarding_doctor [--json] [--expect-event KEY ...]` | Healthcheck: is the bridge attached? handlers registered? active term? table reachable? Non-zero exit on failure — wire into monitoring. |
| `onboarding_inspect <id\|email> [--term CODE] [--json]` | Pretty-prints all onboarding rows + steps for a student. |
| `onboarding_dispatch <event_key> --student <id\|email>` | Fires an event for named students. `--all [--yes]` for fleet-wide. Supports `--dry-run`. |
| `onboarding_complete <id\|email> <step_key> [--status completed\|not_applicable] [--term CODE] [--message "…"]` | Escape hatch — force a step's status when the event pipeline missed a transition. |

### Typical workflows

- **Staff wants a daily dashboard of onboarding:** `aggregate_onboarding_stats` runs overnight via CronTab; the tabs on `/ce/students/` read from `DailyOnboardingStats` (cheap) + live `StudentOnboarding` queries.
- **New nightly reminder schedule:** admin toggles `is_active=Yes` on `/ce/settings/…/student_regis_pending/`, checks the missing-items boxes, sets `freq` + `cron`. The cron entry runs `notify_students_signatures` which forwards to `notify_pending_onboarding`.
- **First-time deploy with existing students:** run `seed_onboarding` once so each active-term student has an onboarding row populated with their current progress.

## How it works

```
producer view ──▶ onboarding_event.send(event='ferpa_completed', student=…)
                              │
                              ▼
            ┌────────────────────────────────┐
            │ StudentOnboardingConfig.ready()│
            │   bridge receiver:             │
            │   handlers.dispatch(event, …)  │
            └────────────────────────────────┘
                              │
                              ▼
            handler registered by any app via
            handlers.register('ferpa_completed', fn)
                              │
                              ▼
                    api.complete_step(...)
                              │
                              ▼
            StudentOnboarding / StudentOnboardingStep
                              │         │
                              │         └─► DailyOnboardingStats (via nightly rollup)
                              ▼
            context_processor → student templates
            DRF endpoints       → CE staff tabs
            notify command      → admin emails
```

## Public API

```python
from student_onboarding import api, events, handlers
from student_onboarding.signals import onboarding_event
from student_onboarding.step_registry import StepDefinition, register, all_steps, notifiable_steps
```

### `step_registry`

| Function | Purpose |
|---|---|
| `register(step: StepDefinition)` | Idempotent — later registrations with the same key overwrite. Call from `AppConfig.ready()`. |
| `all_steps()` | All registered steps sorted by `(order, key)`. |
| `get(key)` | Lookup a single step by key. |
| `notifiable_steps()` | Subset with non-empty `notify_label` — the settings form choices. |

### `StepDefinition` fields

| Field | Purpose |
|---|---|
| `key` | Unique string, used as `StudentOnboardingStep.key`. |
| `label` | Human label shown on the student's checklist and in notification emails. |
| `order` | Display order for the progress card / stepper. |
| `url_name`, `message` | Optional — passed through to `add_step`. |
| `seeded_when(student) -> bool` | Optional — if returns False, step is skipped when seeding defaults (e.g. `verify_email` for an already-verified student). |
| `complete_when(student, term) -> bool` | Optional — read by `seed_onboarding` to pre-mark the step done for students in existing state. |
| `complete_message` | Message to stamp onto the step when the completion event fires. |
| `notify_label` | Non-empty = this step is offered as a checkbox on the admin notification settings page. Blank = tracked but not notifiable. |

### `api`

| Function | Purpose |
|---|---|
| `get_or_create_for_current_term(student, term=None)` | Returns the `StudentOnboarding` for the active (or given) term. |
| `add_step(student, *, key, label, url_name='', message='', order=100, status='pending', term=None)` | Idempotent — never duplicates `(student, term, key)`. Increments `total_steps` only on actual create. |
| `complete_step(student, *, key, message=None, term=None)` | No-op when the step doesn't exist for the term. Stamps `completed_on`, increments `completed_steps`, calls `recompute_completion()`. |
| `mark_not_applicable(student, *, key, term=None)` | Same accounting as `complete_step`. |

All counter mutations are wrapped in `transaction.atomic()` with `select_for_update()` on the parent row.

### `signals` + `handlers` + `events`

A single Django signal (`onboarding_event`) carries `event` (string) and `student`. The bridge in `apps.py` fans out to handlers registered via `handlers.register(event, fn)`.

Built-in event names:

| Constant | String |
|---|---|
| `events.APPLICATION_STARTED` | `'application_started'` |
| `events.FERPA_COMPLETED` | `'ferpa_completed'` |
| `events.CLASSES_APPLIED` | `'classes_applied'` |
| `events.PROFILE_VERIFIED` | `'profile_verified'` |
| `events.EMAIL_VERIFIED` | `'email_verified'` |
| `events.STUDENT_AGREEMENT_SIGNED` | `'student_agreement_signed'` |
| `events.TUITION_ASSISTANCE_SUBMITTED` | `'tuition_assistance_submitted'` |

### Models

- **`StudentOnboarding`** — `student`, `term`, `started_on`, `completed_on`, `last_notified_on`, `total_steps`, `completed_steps`, `progress_percent` (property).
- **`StudentOnboardingStep`** — `key`, `label`, `url_name`, `message`, `status` (`pending` / `completed` / `not_applicable`), `order`, `completed_on`, `is_done` (property).
- **`DailyOnboardingStats`** — one row per `(date, term, highschool, step_key)`; powers the timeline + by-HS + funnel endpoints.

### Context processor + template tag

- `student_onboarding.context_processors.onboarding_progress` — injects `onboarding` into every authenticated student request.
- `{% load student_onboarding %}{% step_nav_class step current_task %}` — returns `step-completed` / `step-active` / `step-upcoming`.

## Modifying the step list as needs change

The step list is **data**, not code branches scattered across the app. One registry feeds the student's progress card, the CE staff funnel + by-student table + notifier + backfill, and the admin settings page. Everything below happens by editing `STEPS` in `webapp/cis/signals/onboarding.py` (or its equivalent in another host app). No template or other-app edits required for any of these.

### Scenario A — Add a new step (example: payment is now required)

Suppose starting next term, every student must complete a $50 tuition deposit before their onboarding counts as complete. You want:
- The step to appear in the student progress card with a link to the payment page.
- A new event (`PAYMENT_MADE`) to automatically mark the step complete.
- Existing students who've already paid to be pre-marked complete on backfill.
- Admins to be able to email students who haven't paid.

Steps:

1. **Declare an event name** in the host or owning app:

   ```python
   # webapp/invoice/invoice/events.py  (or cis/events.py)
   PAYMENT_MADE = 'payment_made'
   ```

2. **Add a `StepDefinition`** to `STEPS` in `cis/signals/onboarding.py`:

   ```python
   STEPS.append(StepDefinition(
       key='pay_tuition',
       label='Pay tuition deposit',
       order=45,
       url_name='student:pay_deposit',
       message='A $50 deposit is required to confirm enrollment.',
       seeded_when=None,                              # always seed
       complete_when=lambda s, term: s.has_paid_deposit(term),   # backfill predicate
       complete_message='Tuition deposit paid.',
       notify_label='Notify students who have not paid the tuition deposit',
   ))
   ```

3. **Wire the event to the completion handler** in `EVENT_TO_STEP_KEY`:

   ```python
   from invoice.events import PAYMENT_MADE

   EVENT_TO_STEP_KEY[PAYMENT_MADE] = 'pay_tuition'
   ```

   (Or register the handler directly via `handlers.register(PAYMENT_MADE, …)` if the event is owned by another app.)

4. **Dispatch the event** from wherever a payment succeeds:

   ```python
   # webapp/invoice/views/pay.py
   from student_onboarding.signals import onboarding_event
   from invoice.events import PAYMENT_MADE

   onboarding_event.send(sender=__name__, event=PAYMENT_MADE, student=student)
   ```

5. **Backfill + re-aggregate** so existing data matches the new step:

   ```bash
   docker exec -w /app/webapp django_web_ewu python manage.py seed_onboarding
   docker exec -w /app/webapp django_web_ewu python manage.py aggregate_onboarding_stats --backfill-from 2026-01-01
   ```

That's it. The student progress card, funnel chart, by-student datatable, notification settings page, and backfill command all pick up the new step automatically.

### Scenario B — Remove a step

Two paths depending on intent:

**To hide the step but keep historical data:**
- Delete the `StepDefinition` from `STEPS`.
- Drop its entry from `EVENT_TO_STEP_KEY` (and remove any dispatches of the event if that event is now unused).
- Existing `StudentOnboardingStep` rows with that key remain in the DB for audit. They stop appearing on new students (because the seed no longer adds them) but old students still see them on their card until the term rolls over. If that's unacceptable, run a one-shot:

  ```bash
  docker exec -w /app/webapp django_web_ewu python manage.py shell -c "
  from student_onboarding.models import StudentOnboardingStep
  StudentOnboardingStep.objects.filter(key='pay_tuition').update(status='not_applicable')
  "
  ```

**To gate a step so it only applies to some students:** don't remove it — give it a `seeded_when` predicate. See Scenario D.

### Scenario C — Rename or relabel a step

- `label` change only (user-visible text): edit the `StepDefinition.label`. Existing rows keep the **old** label because `label` is copied onto the step at creation time. Run a one-shot `StudentOnboardingStep.objects.filter(key=…).update(label='New label')` if you want retroactive.
- `key` change (database identity): avoid. If you must, add the new key as a fresh `StepDefinition`, run a backfill migration `StudentOnboardingStep.objects.filter(key='old').update(key='new')`, then delete the old `StepDefinition`.

### Scenario D — Make a step conditional on student state

Use `seeded_when`. The predicate receives the student and returns `True` to include the step:

```python
# tuition_assistance only seeded for qualifying students
StepDefinition(
    key='tuition_assistance',
    ...,
    seeded_when=lambda s: bool(getattr(s, 'qualify_tuition_assistance', False)),
)

# verify_info only seeded for returning students
StepDefinition(
    key='verify_info',
    ...,
    seeded_when=_is_returning,   # reuse the module's predicate
)
```

`seeded_when` is checked at `APPLICATION_STARTED` time and on term rollover (via `user_logged_in`), so changing the predicate affects new / rolled-over onboardings but not ones already seeded.

### Scenario E — Change notification behavior

- **Opt a step into email notifications:** set `notify_label='…short admin-facing description…'` on its `StepDefinition`. Next page load of the settings page exposes a new checkbox. Admins must also tick it and click Save.
- **Opt out:** set `notify_label=''`. The checkbox disappears; any previously-saved value for that key in the `missing_items` list is silently ignored.
- **Change the email subject/body, cadence (`freq`), or debug recipients:** all live on the `student_regis_pending` setting in `/ce/settings/…`, not in code.
- **Temporarily pause notifications:** set `is_active=No` (or `Debug` to redirect to staff) in the settings page. The command short-circuits.

### Scenario F — Reorder steps

Edit `order` on the `StepDefinition`. Affects display only (the progress card and chevron stepper iterate by `order, id`). No counter implications. Existing rows already have their old `order` stored; if you want the change retroactive:

```python
StudentOnboardingStep.objects.filter(key='pay_tuition').update(order=45)
```

### Scenario G — New field on the student that should block onboarding

E.g. "must acknowledge a new COVID waiver this term." Add a `covid_waiver` step with:
- A `seeded_when` predicate gated on the active term being the one you want to enforce from,
- A new event (`COVID_WAIVER_ACKNOWLEDGED`),
- A dispatch from the waiver-acknowledgment view.

Follow Scenario A.

---

### Step-definition anatomy cheat sheet

```
StepDefinition(
    key              = 'machine-readable unique id, used as DB key',
    label            = 'shown to the student on the checklist + in emails',
    order            = display order (int; lower = earlier),
    url_name         = Django URL name to link from the checklist (optional),
    message          = default checklist message (optional),
    seeded_when      = predicate(student) -> bool; None = always seed,
    complete_when    = predicate(student, term) -> bool; used by seed_onboarding,
    complete_message = message stamped onto the step when completion fires,
    notify_label     = admin checkbox label; blank = not notifiable,
)
```

## Adding a step from another app

Because steps now live in a registry, another app can contribute one without touching `cis/signals/onboarding.py`:

```python
# myapp/apps.py
class MyAppConfig(AppConfig):
    name = 'myapp'
    def ready(self):
        from student_onboarding.step_registry import StepDefinition, register
        from student_onboarding import handlers, events as oe
        from student_onboarding.api import complete_step
        from .events import PAYMENT_MADE

        register(StepDefinition(
            key='pay_tuition',
            label='Pay tuition deposit',
            url_name='myapp:pay',
            order=40,
            message='A $50 deposit is required to confirm enrollment.',
            complete_when=lambda student, term: student.has_paid_deposit(term),
            complete_message='Tuition paid.',
            notify_label='Notify students who have not paid the tuition deposit',
        ))

        handlers.register(PAYMENT_MADE, lambda s, **_: complete_step(
            s, key='pay_tuition', message='Tuition paid.',
        ))
```

The new step now shows up in: the student's progress card, the CE staff funnel chart, the backfill command (if `complete_when` is set), and the admin notification settings page (if `notify_label` is set). No host-app edits.

## Admin

The app registers both `StudentOnboarding` and `StudentOnboardingStep` in Django admin, with step inlines on the parent, term/status filters, and email/psid search — useful for read-only support access without giving shell.

## Tests

```bash
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```
