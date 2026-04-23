# Plan: admin/debug management commands

## Context

Operators have been copy-pasting Python snippets into the prod Django shell to diagnose and repair onboarding state (did the bridge attach? which handlers are registered? reseed this student; complete this step; inspect a student's rows). That's error-prone and leaves no audit trail. This plan adds four first-class `python manage.py` commands so every operation is scriptable, self-documenting, and usable by non-Python operators.

Existing:
- `seed_onboarding` — bulk-seed for existing students (keep as-is).

New commands proposed: `onboarding_doctor`, `onboarding_dispatch`, `onboarding_inspect`, `onboarding_complete`.

All four commands live in `student_onboarding/management/commands/` and follow the package's existing conventions (no Django-version-specific APIs, `--dry-run` where applicable, exit code 1 on error, terse default output + `-v 2` for detail).

---

## 1. `onboarding_doctor` — health check

**Purpose.** First command an operator runs when something seems off. Tells you whether the plumbing itself is healthy before you bother debugging specific students.

**Behavior.** Prints a checklist with ✓/✗ per item and exits non-zero if any fail:
- `StudentOnboardingConfig.ready()` ran → `apps.get_app_config('student_onboarding').__class__` is one of the package's configs.
- Bridge receiver connected → `onboarding_event.receivers` is non-empty and at least one entry points at the bridge.
- Handlers registry populated → `handlers.registered_events()` has entries for every event in `events` (configurable).
- Active term resolvable → `cis.utils.active_term()` returns a Term.
- `StudentOnboarding` table exists → `StudentOnboarding.objects.exists()` runs without error.
- Summary counts: total onboardings, total steps, per-status breakdown, students missing onboarding for active term.

**CLI.**
```
python manage.py onboarding_doctor [--json] [--expect-event KEY ...]
```

- `--json`: machine-readable output for CI/monitoring.
- `--expect-event`: assert specific event keys are registered (e.g. `--expect-event ferpa_completed classes_applied payment_made`). Fails loud if any missing.

**Why it helps.** Replaces the five-step manual diagnostic conversation (`onboarding_event.receivers`, `handlers.registered_events()`, `apps.get_app_config(...)`, etc.) with one command whose output can be pasted back verbatim.

---

## 2. `onboarding_dispatch` — fire an event for one or many students

**Purpose.** Manually dispatch any registered event without dropping into the shell. Used to:
- Backfill completion after a deploy where events were silently dropped.
- Test that a newly-wired handler actually fires.
- Reseed a single student whose steps are missing.

**Behavior.** Dispatches `onboarding_event.send(event=<event>, student=<student>)` for each selected student.

**CLI.**
```
python manage.py onboarding_dispatch <event_key> \
    [--student <id|email>] [--all] [--dry-run] [--limit N] [--verbose]
```

- Positional `event_key`: e.g. `application_started`, `ferpa_completed`, `classes_applied`, `payment_made`, `profile_verified` — anything in the handlers registry. Unknown key → error with the list of known keys.
- `--student`: UUID **or** email; repeatable. Looks up `Student.objects.get(pk=... | user__email=...)`.
- `--all`: dispatch for every student; mutually exclusive with `--student`. Required safety flag to prevent fat-finger mass fire.
- `--dry-run`: log who would be targeted, dispatch nothing.
- `--verbose`: log each dispatch; default is summary line only.

**Example.**
```
# Backfill FERPA completion for one student
python manage.py onboarding_dispatch ferpa_completed --student avi+chs1@canusia.com

# Fleet-wide reseed after a bridge outage
python manage.py onboarding_dispatch application_started --all
```

**Why it helps.** Today, operators paste a 3-line snippet into the shell. This replaces it with one auditable command that can be scripted, rate-limited, and logged.

---

## 3. `onboarding_inspect` — show a student's onboarding state

**Purpose.** Pretty-print all onboarding rows + steps for a student, across terms.

**CLI.**
```
python manage.py onboarding_inspect <student_id_or_email> [--term CODE] [--json]
```

**Example output (text mode).**
```
Student: avi+chs1@canusia.com (a7db…)  app_status=accepted

Term 4391   started=2026-04-23   completed_on=-   3/4 steps
  ✓ ferpa          completed    2026-04-23 10:15
  · classes        pending      -
  · pay_tuition    pending      -                 balance=$150.00
  · verify_info    pending      -

Term 4401   started=2026-01-02   completed_on=2026-02-10   3/3 steps
  ✓ ferpa          completed    2026-01-02 09:00
  ✓ classes        completed    2026-01-03 14:00
  ✓ pay_tuition    completed    2026-02-10 11:30
```

**Why it helps.** Replaces `StudentOnboardingStep.objects.filter(...).values(...)` ad-hoc queries; also surfaces the current balance + per-step timestamps without knowing the model layout.

---

## 4. `onboarding_complete` — mark a step completed/not_applicable for a student

**Purpose.** Escape hatch for when an event was missed and the operator needs to move the step manually (e.g. legacy FERPA submission never fired the event).

**CLI.**
```
python manage.py onboarding_complete <student_id_or_email> <step_key> \
    [--term CODE] [--status completed|not_applicable] [--message "…"]
```

- Wraps `student_onboarding.api.complete_step` / `mark_not_applicable`.
- Default `--status completed`.
- Records `--message` on the step (visible in `onboarding_inspect`).
- Errors if the step doesn't exist — points the operator at `onboarding_dispatch application_started` first.

**Why it helps.** Gives ops a supported way to correct state without touching the ORM.

---

## Shared concerns

### Student lookup helper
All three student-taking commands need the same "UUID or email" resolver. Introduce `student_onboarding/management/_lookup.py` with `resolve_student(value) -> Student`:

```python
def resolve_student(value):
    if '@' in value:
        return Student.objects.get(user__email=value)
    return Student.objects.get(pk=value)
```

Keeps behavior consistent and error messages uniform.

### Output
Every command supports `--json` for machine-readable output. Default is human-readable with the Django `self.style.*` colors the seed command already uses.

### Permissions / dangerous flags
`--all` on `onboarding_dispatch` must be paired with a prompt unless `--yes` is given (pattern: `input('Dispatch "<event>" to <N> students? [y/N] ')`). Scripts pass `--yes`.

### Tests
Each command gets a minimal `tests/test_commands.py`:
- `onboarding_doctor` returns 0 on a healthy test app; returns 1 when we forcibly disconnect the bridge.
- `onboarding_dispatch ferpa_completed --student X` flips the step.
- `onboarding_inspect X` emits the student's step keys.
- `onboarding_complete X ferpa` flips the step; errors cleanly when step absent.

Reuse whatever test fixtures `seed_onboarding` tests already set up (check `tests/` directory — if absent, add a `tests/conftest.py` with a student + term + onboarding factory).

---

## Release

Target version: **v0.0.5** (commands are additive, no breaking changes).

Changelog entry:
```
## v0.0.5

- Add management commands: onboarding_doctor, onboarding_dispatch,
  onboarding_inspect, onboarding_complete. Together with seed_onboarding
  these cover the full operational surface without shell access.
- No schema changes.
```

## Implementation order

1. `_lookup.py` helper + one-off tests.
2. `onboarding_inspect` (read-only, easiest, proves the lookup + formatting scaffolding).
3. `onboarding_doctor` (read-only, no student loop).
4. `onboarding_dispatch` (write, reuses lookup + dispatch).
5. `onboarding_complete` (write, reuses lookup + API).
6. Update `README.md` "Ops runbook" section listing all five commands with one-line descriptions.
7. Tag v0.0.5.
