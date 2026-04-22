# student_onboarding

Per-term student onboarding checklist for the MyCE student portal.

Tracks the steps a student must complete for the active term (FERPA, class application, returning-student profile review, plus any steps registered by other apps), surfaces them through a sidebar progress card on the dashboard and a chevron stepper at the top of each task page.

Distributed as a Django app + pip-installable package using the same submodule pattern as `myce_invoice`. See `CLAUDE.md` for conventions and gotchas.

## Install

### Production (pip)

```bash
pip install git+https://github.com/Canusia/package-student_onboarding.git@v0.0.1
```

```python
# settings.py
INSTALLED_APPS += ['student_onboarding.apps.StudentOnboardingConfig']
TEMPLATES[0]['OPTIONS']['context_processors'].append(
    'student_onboarding.context_processors.onboarding_progress'
)
```

### Dev (submodule)

```bash
git submodule add https://github.com/Canusia/package-student_onboarding.git webapp/student_onboarding
```

```python
# settings.py — auto-detect
import importlib.util
INSTALLED_APPS += [
    'student_onboarding.student_onboarding.apps.DevStudentOnboardingConfig'
    if importlib.util.find_spec('student_onboarding.student_onboarding')
    else 'student_onboarding.apps.StudentOnboardingConfig',
]
```

Consumer code (`from student_onboarding.api import add_step`, `{% load student_onboarding %}`) is identical in both modes thanks to outer-level proxy modules.

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

## Tests

```bash
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```
