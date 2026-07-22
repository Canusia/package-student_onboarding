# CLAUDE.md — `student_onboarding` package

Guidance for working in this app. Repo-wide rules in `/repos/ewu/CLAUDE.md` still apply (Docker, `docker exec`, etc.).

## Package layout (submodule pattern)

```
webapp/student_onboarding/             ← submodule root
├── pyproject.toml, setup.cfg, MANIFEST.in, LICENSE
├── README.md, CLAUDE.md
├── __init__.py                        (empty marker)
├── api.py, events.py, handlers.py, signals.py,
│   context_processors.py, models.py, step_registry.py,
│   urls.py                            ← outer proxies re-exporting from inner
├── settings/
│   ├── __init__.py
│   └── student_regis_pending.py       ← outer proxy
└── student_onboarding/                ← actual Django app
    ├── apps.py                        (StudentOnboardingConfig + DevStudentOnboardingConfig)
    ├── admin.py, models.py, signals.py, events.py, handlers.py, api.py,
    │   context_processors.py, tests.py, step_registry.py, urls.py, views.py,
    │   serializers.py
    ├── settings/student_regis_pending.py
    ├── templatetags/student_onboarding.py
    ├── templates/student_onboarding/ce/    (staff tabs)
    ├── management/commands/
    │   ├── seed_onboarding.py
    │   ├── notify_pending_onboarding.py
    │   ├── aggregate_onboarding_stats.py
    │   ├── onboarding_doctor.py, onboarding_inspect.py,
    │   │   onboarding_dispatch.py, onboarding_complete.py
    └── migrations/
```

Same shape as `webapp/invoice/`. Two app configs:
- **`StudentOnboardingConfig`** (`name='student_onboarding'`) — production (pip-installed).
- **`DevStudentOnboardingConfig`** (`name='student_onboarding.student_onboarding'`) — dev (submodule checkout).

Settings selects via `importlib.util.find_spec('student_onboarding.student_onboarding')`. Both yield Django app label `student_onboarding`, so model FKs (`'student_onboarding.StudentOnboarding'`) and migrations are stable across modes.

## Why the outer proxy modules

Cross-app code writes `from student_onboarding.api import add_step`. In dev, `api.py` lives at `student_onboarding.student_onboarding.api`, not `student_onboarding.api`. A lazy/PEP-562 shim doesn't work here because Python's `from pkg.X import Y` syntax bypasses module-level `__getattr__`. The fix: tiny proxy files at the outer level that `from .student_onboarding.X import *`.

**Don't add eager imports in the outer `__init__.py`** that touch Django models or `cis.utils` — Django imports the package while resolving `INSTALLED_APPS`, before the app registry is populated, and you'll get `AppRegistryNotReady`.

Anytime you add a new top-level module to the inner app that other apps need to import (e.g. a new `serializers.py`, `urls.py`, `step_registry.py`), add a matching proxy file in the outer dir.

## What this app is

Infrastructure for a per-term onboarding checklist:
- **Models** — `StudentOnboarding` (parent, per `student` × `term`), `StudentOnboardingStep` (rows), `DailyOnboardingStats` (rollup for reporting).
- **Generic signal + handler registry** — one `onboarding_event = Signal()` carries `event` (string) and `student`. Apps register handlers via `handlers.register(event_name, fn)`.
- **Step registry** — `step_registry.register(StepDefinition(...))`. Host apps declare steps here in `AppConfig.ready()`. The settings form, seeding, backfill, notifier, and reporting all read from this registry.
- **Context processor** — `onboarding` is injected into every authenticated student request.
- **Notification job** — `notify_pending_onboarding` walks pending steps and emails students based on admin settings.
- **CE staff DRF endpoints + templates** — `/ce/onboarding/api/…` viewsets and the five tab partials under `templates/student_onboarding/ce/`.
- **No domain knowledge** — FERPA / classes / TA / agreement predicates live in `myce_tenant_configs.services.onboarding_steps`, not here.

## What this app is NOT

- **Not a step catalog the student-facing templates care about.** Visual partials (`_term_progress.html`, `_term_step_nav.html`) live in `webapp/student/templates/student/partials/` so each portal/repo can style them differently.
- **Not a hardcoded list of steps.** The canonical list is the runtime registry populated by host apps. Nothing in this app enumerates domain steps.

## Hard rules

- **All counter mutations go through `api.add_step` / `complete_step` / `mark_not_applicable`.** They run inside `transaction.atomic()` with `select_for_update()` on the parent. Direct `.save()` on a step will desync `completed_steps`.
- **`api.add_step` is idempotent** (unique on `(onboarding, key)`). Don't add a "does it exist?" check at call sites — that's the helper's job.
- **`api.complete_step` is a no-op when the step doesn't exist for the current term.** Intentional — handlers fire safely even for students who skipped a step.
- **Never define a new `Signal()` for a new event.** Use a string constant in your app's `events.py` and dispatch through `onboarding_event`.
- **Register handlers AND step definitions from `AppConfig.ready()`, not at import time.** This avoids `AppRegistryNotReady` and keeps the registry deterministic.
- **Steps are defined, not hardcoded.** When you'd reach for an `if` in `_seed_default_steps`, instead add a `seeded_when` predicate on the `StepDefinition`. When you'd hardcode a completion check for backfill, use `complete_when`. When you'd hardcode a mapping from a settings key to a step, just set `notify_label` and let the form pick it up.
- **All internal imports inside the inner package use relative form** (`from .models import …`) so they work in both prod and dev.

## Step registry conventions

`StepDefinition` is the single source of truth. Fields:

| Field | Purpose |
|---|---|
| `key` | unique string, also the DB key on `StudentOnboardingStep` |
| `label` | shown to the student AND used as the missing-item label in notification emails |
| `order` | display order for the checklist / stepper |
| `url_name`, `message` | passed through to `add_step` |
| `seeded_when(student) -> bool` | if set and returns False, step is skipped during default seeding |
| `complete_when(student, term) -> bool` | read only by `seed_onboarding` (backfill) |
| `complete_message` | stamped onto the step when the matching event fires |
| `notify_label` | non-empty = admin can toggle email notifications for this step on `/ce/settings/…/student_regis_pending/` |

When adding a step, **the only file you should need to edit is the host app's `myce_tenant_configs.services.onboarding_steps`** (or equivalent). Nothing in `student_onboarding` itself should hardcode step keys.

## Settings form

`settings/student_regis_pending.py` is the moved-in settings form. Key points:

- `Setting` DB key is `{CAMPUS_CODE_PREFIX}_student_regis_email` — unchanged from the legacy cis-owned version, so old values keep loading.
- `missing_items` choices are **built in `__init__` from `notifiable_steps()`** — do NOT hardcode them.
- Saved values are step keys (e.g. `'ferpa'`). A legacy alias map in `notify_pending_onboarding` translates old tokens (`'missing_ferpa'` → `'ferpa'`, etc.) so settings saved before the refactor still work.
- The CE settings machinery still discovers this form via `CONFIGURATORS` in `cis/apps.py` (`'name': 'student_regis_pending'`). A back-compat shim at `cis/settings/student_regis_pending.py` re-exports so imports of the old path still work.

## Notification command

`notify_pending_onboarding`:

- Reads `missing_items` from the setting; normalizes via `LEGACY_MISSING_ITEM_ALIASES`.
- Walks `StudentOnboarding.objects.filter(term=active_term(), completed_on__isnull=True)`.
- Filters each student's pending steps to the admin-selected keys.
- Rate-limits via `StudentOnboarding.last_notified_on` vs `freq` days.
- Supports `--dry-run` and `--student <id>`.
- Returns a summary with per-step counts; labels pulled live from the registry.
- `notify_students_signatures` is a thin shim that forwards to this command so the existing CronTab entry keeps working.

## Daily rollup

`aggregate_onboarding_stats` — idempotent per `(date, term, highschool, step_key)` via `update_or_create`. Writes:
- one "all-HS" row per term per date with cumulative `started_count` / `completed_count`;
- one per-HS row per term per date with the same;
- one per-step-key row per term per date with `step_completed_count`.

`post_migrate` in `apps.py` registers the CronTab entry (`0 2 * * *`) idempotently. To disable, delete the row from `cis_crontab` and remove the `post_migrate` hook.

## Returning-student detection

`myce_tenant_configs.services.onboarding_steps._is_returning(student)` — true if the student has any `StudentRegistration` outside the current term, or any prior `StudentOnboarding`. Used as the `seeded_when` predicate on `verify_info`. Tighten it in `myce_tenant_configs`, not here.

## Term rollover

`reseed_on_term_rollover` listens to Django's `user_logged_in`. On each login it calls `get_or_create_for_current_term` (creates a new `StudentOnboarding` row when `active_term()` returns a new term) and re-seeds defaults if the new row has no steps. Don't move this into the generic event bus — it's a Django auth signal, not an onboarding event.

## Tests

```bash
docker exec -w /app/webapp django_web_ewu python manage.py test student_onboarding
```

The container working directory is `/app/webapp` (the `webapp/manage.py` form in the repo CLAUDE.md is wrong for this container — use `-w /app/webapp` and `python manage.py ...`).

When patching `active_term` in tests, patch the **inner** path `student_onboarding.student_onboarding.api.active_term` (and `myce_tenant_configs.services.onboarding_steps.active_term` separately).

Mocking the `user_logged_in` signal in tests must include `HTTP_USER_AGENT` on the request because `django_login_history` reads it.

## Common pitfalls

- **Adding a step at any time** — fine, but the new step starts as `pending`, which un-completes the parent (`recompute_completion` will clear `completed_on`). That's correct behavior; just be aware.
- **Changing a step's `label`** — existing rows keep the old label (it's copied at create time). Run a one-shot update if retroactive.
- **Forgetting to set `complete_when`** — `seed_onboarding` will still seed the step but won't pre-mark it complete for students who've already done the action. Add a predicate.
- **Forgetting to set `notify_label`** — the step won't appear as an admin-configurable notification. That's fine for tracked-but-silent steps; opt in by setting the label.
- **Hardcoding a step key anywhere in this package** — stop and put the knowledge on a `StepDefinition` instead.
- **Changing `active_term` mid-conversation** — the next login triggers re-seeding. Existing onboardings for older terms are kept (history); they don't auto-archive.
- **Reordering steps after creation** — only affects display (template iterates by `order, id`). No counter implications.
- **Adding new outer proxy modules** — every new inner top-level module needs a matching outer proxy file.
