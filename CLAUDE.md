# CLAUDE.md — `student_onboarding` package

Guidance for working in this app. Repo-wide rules in `/repos/ewu/CLAUDE.md` still apply (Docker, `docker exec`, etc.).

## Package layout (submodule pattern)

```
webapp/student_onboarding/         ← this repo (submodule root)
├── pyproject.toml, setup.cfg, MANIFEST.in, LICENSE
├── README.md, CLAUDE.md
├── __init__.py                    (empty marker)
├── api.py, events.py, handlers.py, signals.py,
│   context_processors.py, models.py
│       ↳ thin proxy modules — re-export from inner
└── student_onboarding/            ← actual Django app
    ├── apps.py                    (StudentOnboardingConfig + DevStudentOnboardingConfig)
    ├── models.py, signals.py, events.py, handlers.py, api.py,
    │   context_processors.py, tests.py
    ├── templatetags/student_onboarding.py
    └── migrations/
```

Same shape as `webapp/invoice/`. Two app configs:
- **`StudentOnboardingConfig`** (`name='student_onboarding'`) — used in production when this package is pip-installed.
- **`DevStudentOnboardingConfig`** (`name='student_onboarding.student_onboarding'`) — used when this submodule is checked out.

Settings selects via `importlib.util.find_spec('student_onboarding.student_onboarding')`. Both yield Django app label `student_onboarding`, so model FKs (`'student_onboarding.StudentOnboarding'`) and migrations are stable across modes.

## Why the outer proxy modules

Cross-app code writes `from student_onboarding.api import add_step`. In dev, `api.py` lives at `student_onboarding.student_onboarding.api`, not `student_onboarding.api`. A lazy/PEP-562 shim doesn't work here because Python's `from pkg.X import Y` syntax bypasses module-level `__getattr__`. The fix: tiny proxy files at the outer level that `from .student_onboarding.X import *`.

**Don't add eager imports in the outer `__init__.py`** that touch Django models or `cis.utils` — Django imports the package while resolving `INSTALLED_APPS`, before the app registry is populated, and you'll get `AppRegistryNotReady`.

## What this app is

Pure infrastructure for a per-term student onboarding checklist:
- **Models** — `StudentOnboarding` (parent, per `student` × `term`) and `StudentOnboardingStep` (rows). Counters `total_steps` / `completed_steps` are denormalized on the parent for cheap progress-bar reads.
- **Generic signal + handler registry** — one `onboarding_event = Signal()` carries `event` (string) and `student`. Apps register handlers via `handlers.register(event_name, fn)`.
- **Context processor** — `onboarding` is injected into every authenticated student request.
- **No domain knowledge** — receivers for FERPA, classes, profile review live in `cis/signals/onboarding.py`. New domains (payments, etc.) bring their own handlers.

## What this app is NOT

- **Not a markup library.** Visual templates (`_term_progress.html`, `_term_step_nav.html`) live in `webapp/student/templates/student/partials/` so each portal/repo can style them differently.
- **Not a step catalog.** Steps are defined by whichever app handles `APPLICATION_STARTED` and calls `add_step(...)`. There is no central registry of steps — only of event handlers.

## Hard rules

- **All counter mutations go through `api.add_step` / `complete_step` / `mark_not_applicable`.** They run inside `transaction.atomic()` with `select_for_update()` on the parent. Direct `.save()` on a step will desync `completed_steps`.
- **`api.add_step` is idempotent** (unique on `(onboarding, key)`). Don't add a "does it exist?" check at call sites — that's the helper's job.
- **`api.complete_step` is a no-op when the step doesn't exist for the current term.** This is intentional — handlers fire safely even for students who skipped a step.
- **Never define a new `Signal()` for a new event.** Use a string constant in your app's `events.py` and dispatch through `onboarding_event`.
- **Register handlers from `AppConfig.ready()`, not at import time.**
- **All internal imports inside the inner package use relative form** (`from .models import …`) so they work in both prod and dev.

## Adding a new event from another app

```python
# myapp/events.py
PAYMENT_MADE = 'payment_made'

# myapp/handlers.py
from student_onboarding.api import add_step, complete_step

def on_application_started(student, **kwargs):
    add_step(student, key='pay_tuition', label='Pay tuition',
             url_name='myapp:pay', order=40)

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

# producer (any view)
from student_onboarding.signals import onboarding_event
from myapp.events import PAYMENT_MADE
onboarding_event.send(sender=__name__, event=PAYMENT_MADE, student=student)
```

No changes required in this app.

## Returning-student detection

`cis/signals/onboarding._is_returning(student)` — true if the student has any `StudentRegistration` outside the current term, or any prior `StudentOnboarding`. Used to gate the `verify_info` step. Tighten it in cis, not here.

## Term rollover

`reseed_on_term_rollover` listens to Django's `user_logged_in`. On each login it calls `get_or_create_for_current_term` (creates a new `StudentOnboarding` row when `active_term()` returns a new term) and re-seeds defaults if the new row has no steps. Don't move this into the generic event bus — it's a Django auth signal, not an onboarding event.

## Tests

```bash
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```

The container working directory is `/app/webapp` (the `webapp/manage.py` form in the repo CLAUDE.md is wrong for this container — use `-w /app/webapp` and `python manage.py ...`).

When patching `active_term` in tests, patch the **inner** path `student_onboarding.student_onboarding.api.active_term` (and `cis.signals.onboarding.active_term` separately).

Mocking the `user_logged_in` signal in tests must include `HTTP_USER_AGENT` on the request because `django_login_history` reads it.

## Common pitfalls

- **Adding a step at any time** — fine, but the new step starts as `pending`, which un-completes the parent (`recompute_completion` will clear `completed_on`). That's correct behavior; just be aware.
- **Changing `active_term` mid-conversation** — the next login triggers re-seeding. Existing onboardings for older terms are kept (history); they don't auto-archive.
- **Reordering steps after creation** — only affects display (template iterates by `order, id`). No counter implications.
- **Adding new outer proxy modules** — anytime you add a new top-level module to the inner app that other apps need to import (e.g. a new `urls.py`), add a matching proxy file in the outer dir.
