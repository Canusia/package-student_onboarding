# student_onboarding

Per-term student onboarding checklist for the MyCE student portal.

Tracks the steps a student must complete for the active term (FERPA, class application, returning-student profile review, plus any steps registered by other apps), surfaces them through a sidebar progress card on the dashboard and a chevron stepper at the top of each task page.

Distributed as a Django app + pip-installable package using the same submodule pattern as `myce_invoice`. See `CLAUDE.md` for conventions and gotchas.

## Install

The package is dual-mode: pip-installed in production, git submodule in dev. Consumer code (`from student_onboarding.api import add_step`, `{% load student_onboarding %}`) is identical in both modes thanks to outer-level proxy modules.

### Step 1 — Get the code

**Production (pip):**

Add to your `requirements.txt`:

```
git+https://github.com/Canusia/package-student_onboarding.git@v0.0.1
```

**Dev (git submodule):**

```bash
cd <host-repo-root>
git submodule add https://github.com/Canusia/package-student_onboarding.git webapp/student_onboarding
git submodule update --init --recursive
```

### Step 2 — Wire into Django settings

Add the app config and the context processor. Use the auto-detect form so the same `settings.py` works in both modes:

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

The Django app label is `student_onboarding` in both modes, so model FKs (`'student_onboarding.StudentOnboarding'`) and migrations are stable.

### Step 3 — Run migrations

```bash
python manage.py migrate student_onboarding
```

Creates `student_onboarding_studentonboarding` and `student_onboarding_studentonboardingstep` tables.

### Step 4 — Register the built-in step handlers

This package ships **no domain knowledge** (no FERPA / class registration / profile review code). It expects the host project to register handlers for the events its views dispatch. The MyCE host repos do this in `cis/signals/onboarding.py`:

```python
# webapp/cis/signals/onboarding.py
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from student_onboarding import handlers, events
from student_onboarding.api import add_step, complete_step, get_or_create_for_current_term

from cis.models.student import Student
from cis.utils import active_term


def _seed_default_steps(student):
    add_step(student, key='ferpa', label='Complete FERPA release',
             url_name='student:ferpa', order=10)
    add_step(student, key='classes', label='Apply for classes',
             url_name='student:classes', order=20)
    # ...optionally verify_info for returning students


def on_application_started(student, **kwargs):
    _seed_default_steps(student)

def on_ferpa(student, **kwargs):
    complete_step(student, key='ferpa', message='FERPA release signed.')

def on_classes(student, **kwargs):
    complete_step(student, key='classes', message='Class selection submitted.')

def on_profile(student, **kwargs):
    complete_step(student, key='verify_info', message='Profile reviewed.')


def register_handlers():
    handlers.register(events.APPLICATION_STARTED, on_application_started)
    handlers.register(events.FERPA_COMPLETED, on_ferpa)
    handlers.register(events.CLASSES_APPLIED, on_classes)
    handlers.register(events.PROFILE_VERIFIED, on_profile)


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
        # ...existing imports...
        from cis.signals import onboarding as onboarding_signals
        onboarding_signals.register_handlers()
```

### Step 5 — Dispatch events from your views

```python
# in any view, after the relevant action succeeds
from student_onboarding.signals import onboarding_event
from student_onboarding import events as onboarding_events

# new student account:
onboarding_event.send(sender=__name__,
                      event=onboarding_events.APPLICATION_STARTED,
                      student=student)

# FERPA submitted:
onboarding_event.send(sender=__name__,
                      event=onboarding_events.FERPA_COMPLETED,
                      student=student)

# classes applied:
onboarding_event.send(sender=__name__,
                      event=onboarding_events.CLASSES_APPLIED,
                      student=student)

# profile reviewed for current term:
onboarding_event.send(sender=__name__,
                      event=onboarding_events.PROFILE_VERIFIED,
                      student=student)
```

### Step 6 — Add the visual partials

This package ships no markup — templates live in your portal app so each portal can style independently. Two reference partials live alongside the host repo's MyCE student app at `webapp/student/templates/student/partials/`:

- `_term_progress.html` — sidebar card with progress bar + per-step links. Include in your dashboard sidebar:

  ```django
  {% include "student/partials/_term_progress.html" %}
  ```

- `_term_step_nav.html` — horizontal chevron stepper. Include at the top of each task page:

  ```django
  {% include "student/partials/_term_step_nav.html" %}
  ```

  Each task view sets `current_task` (e.g. `'ferpa'`, `'classes'`, `'verify_info'`) in its render context to highlight the active tile.

Both partials iterate `onboarding.steps.all`, so any step a third app registers (e.g. `pay_tuition` from a payments app) appears automatically with no template edits.

### Verify

```bash
python manage.py check
python manage.py test student_onboarding
```

Browse to the student dashboard. The progress card should appear in the sidebar with the seeded steps in `pending` status. Completing FERPA, classes, or the profile review should flip the corresponding step to `completed` on the next page render.

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
                              │
                              ▼
            context_processor → templates
```

## Public API

```python
from student_onboarding import api, events, handlers
from student_onboarding.signals import onboarding_event
```

### `api`

| Function | Purpose |
|---|---|
| `get_or_create_for_current_term(student, term=None)` | Returns the `StudentOnboarding` for the active (or given) term. |
| `add_step(student, *, key, label, url_name='', message='', order=100, status='pending', term=None)` | Idempotent — never duplicates `(student, term, key)`. Increments `total_steps` only on actual create. |
| `complete_step(student, *, key, message=None, term=None)` | No-op when the step doesn't exist for the term. Stamps `completed_on`, increments `completed_steps`, calls `recompute_completion()`. |
| `mark_not_applicable(student, *, key, term=None)` | Same accounting as `complete_step`. |

All counter mutations are wrapped in `transaction.atomic()` with `select_for_update()` on the parent row.

### `signals` + `handlers` + `events`

A single Django signal (`onboarding_event`) carries `event` (string) and `student`. The bridge receiver in `apps.py` fans out to handlers registered via `handlers.register(event, fn)`.

Built-in event names:

| Constant | String |
|---|---|
| `events.APPLICATION_STARTED` | `'application_started'` |
| `events.FERPA_COMPLETED` | `'ferpa_completed'` |
| `events.CLASSES_APPLIED` | `'classes_applied'` |
| `events.PROFILE_VERIFIED` | `'profile_verified'` |

### Models

- **`StudentOnboarding`** — `student`, `term`, `started_on`, `completed_on`, `total_steps`, `completed_steps`, `progress_percent` (property).
- **`StudentOnboardingStep`** — `key`, `label`, `url_name`, `message`, `status` (`pending` / `completed` / `not_applicable`), `order`, `completed_on`, `is_done` (property).

### Context processor

`student_onboarding.context_processors.onboarding_progress` injects the current `StudentOnboarding` as `onboarding` into every authenticated student request.

### Template tag

```django
{% load student_onboarding %}
{% step_nav_class step current_task %}  {# 'step-completed' | 'step-active' | 'step-upcoming' #}
```

### Templates

The two visual partials live in the **consuming portal app** (here: `webapp/student/templates/student/partials/`), not in this package, so portals can restyle without forking:

- `_term_progress.html` — sidebar card with progress bar, included in `dashboard.html`.
- `_term_step_nav.html` — chevron stepper, included at the top of `ferpa.html`, `classes.html`, `profile.html`. Each task view sets `current_task` (e.g. `'ferpa'`) in its render context.

## Where the built-in steps come from

Defined in the host repo's `cis/signals/onboarding.py`, registered via `register_handlers()` from `cis/apps.py` `ready()`. They handle `APPLICATION_STARTED`, `FERPA_COMPLETED`, `CLASSES_APPLIED`, `PROFILE_VERIFIED` and the term-rollover on `user_logged_in`.

## Adding a step from another app

```python
# myapp/events.py
PAYMENT_MADE = 'payment_made'

# myapp/handlers.py
from student_onboarding.api import add_step, complete_step

def on_application_started(student, **kwargs):
    add_step(student, key='pay_tuition', label='Pay tuition deposit',
             url_name='myapp:pay', order=40,
             message='A $50 deposit is required to confirm enrollment.')

def on_payment(student, **kwargs):
    complete_step(student, key='pay_tuition', message='Tuition paid.')

# myapp/apps.py
class MyAppConfig(AppConfig):
    name = 'myapp'
    def ready(self):
        from student_onboarding import handlers, events as oe
        from . import handlers as my
        from .events import PAYMENT_MADE
        handlers.register(oe.APPLICATION_STARTED, my.on_application_started)
        handlers.register(PAYMENT_MADE, my.on_payment)
```

Producer (any view):

```python
from student_onboarding.signals import onboarding_event
from myapp.events import PAYMENT_MADE

onboarding_event.send(sender=__name__, event=PAYMENT_MADE, student=student)
```

The new step appears automatically in both partials because both iterate `onboarding.steps.all`. No template edits required.

## Ops runbook

All management commands live under `python manage.py`. Wrap with
`docker exec -w /app/webapp <django_container>` as appropriate.

| Command | What it does |
|---|---|
| `onboarding_doctor [--json] [--expect-event KEY ...]` | Healthcheck: is the bridge attached? are handlers registered? active term? table reachable? Exits non-zero on failure — wire into monitoring. |
| `onboarding_inspect <id\|email> [--term CODE] [--json]` | Pretty-prints all onboarding rows + steps for a student, with balance surfaced for `pay_tuition`. |
| `onboarding_dispatch <event_key> --student <id\|email> [--student ...]` | Fires an event for named students. Use `--all [--yes]` for fleet-wide (requires confirmation). Supports `--dry-run`. |
| `onboarding_complete <id\|email> <step_key> [--status completed\|not_applicable] [--term CODE] [--message "…"]` | Escape hatch — manually sets a step's status when the event pipeline missed a transition. |
| `seed_onboarding [--dry-run] [--limit N] [--student ID]` | Backfill onboarding rows + pre-mark FERPA / classes / profile based on existing state. Safe to re-run. |

### Typical recovery workflow

1. `onboarding_doctor` to confirm plumbing (no point debugging data if the bridge is down).
2. `onboarding_inspect <student>` to see the student's current state.
3. `onboarding_dispatch <event> --student <student>` to replay the missed event, or `onboarding_complete` if you need to force state directly.

## Admin

The app registers both `StudentOnboarding` and `StudentOnboardingStep` in Django admin, with step inlines on the parent, term/status filters, and email/psid search — useful for read-only support access without giving shell.

## Tests

```bash
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```
