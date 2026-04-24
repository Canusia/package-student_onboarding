# Plan: Onboarding Summary Tabs on /ce/students/

## Context

The `student_onboarding` submodule tracks per-term checklist progress for every student (FERPA, classes selection, profile verify, etc.), but today CE admins have no way to see aggregate progress. The current UI surfaces onboarding only in the student's own portal via context processor.

This change adds staff-facing visibility on `/ce/students/`: a new set of tabs showing who has onboarded, where they're stuck, which high schools are on top, and how the cohort is trending across the registration→payment window. Dates anchoring the chart come from `cis/settings/registrations.py` (`starting_date` → `tuition_pay_end_date`). All new code lives in `student_onboarding`; `cis/templates/cis/students/index.html` only adds tab buttons and `{% include %}` calls.

## Architecture

```
student_onboarding/student_onboarding/
  models.py                 # + DailyOnboardingStats
  views.py                  # new — staff ViewSets + DRF viewsets
  urls.py                   # new — /ce/onboarding/... endpoints
  serializers.py            # new
  admin.py                  # already added in last commit; extend for new model
  migrations/0002_*.py      # new — DailyOnboardingStats
  templates/student_onboarding/ce/
    _tabs_nav.html          # <li> entries for nav-tabs
    _tab_by_student.html    # DataTable: 1 row per student × active term
    _tab_by_highschool.html # DataTable: 1 row per HS, with % bar
    _tab_timeline.html      # Chart.js line: daily cumulative started vs completed
    _tab_funnel.html        # Chart.js bar: per-step completion counts
    _tab_stalled.html       # DataTable: students with no progress in N days
  management/commands/
    aggregate_onboarding_stats.py   # new — daily rollup, idempotent per date
```

## Data Model

New model `DailyOnboardingStats` in `student_onboarding/models.py`:

```python
class DailyOnboardingStats(models.Model):
    date          = DateField()
    term          = FK('cis.Term', PROTECT)
    highschool    = FK('cis.HighSchool', PROTECT, null=True, blank=True)  # null = "all"
    step_key      = CharField(max_length=64, blank=True)                   # '' = "any step"
    started_count     = PositiveIntegerField(default=0)  # onboardings with started_on <= date
    completed_count   = PositiveIntegerField(default=0)  # onboardings with completed_on <= date
    step_completed_count = PositiveIntegerField(default=0)  # steps w/ completed_on date == date (if step_key set)
    class Meta:
        unique_together = [('date','term','highschool','step_key')]
        indexes = [Index(fields=['date','term']), Index(fields=['term','highschool'])]
```

One row captures either a per-HS or all-HS snapshot per day per term, optionally scoped to a single step key. This lets the timeline, HS leaderboard, and funnel all read from the same table.

## DRF Endpoints (under `/ce/api/onboarding/`)

Register a sub-router in `student_onboarding/urls.py`, then include from `cis/urls.py` (single `path('ce/', include('student_onboarding.urls'))` line).

| Endpoint | Purpose | Pagination |
|---|---|---|
| `GET /ce/api/onboarding/by_student/?format=datatables&term_id=&highschool_id=` | Row per `StudentOnboarding` with progress %, last completed step, days since last progress | datatables server-side |
| `GET /ce/api/onboarding/by_highschool/?format=datatables&term_id=` | Aggregated: total students, started, completed, % per HS | datatables server-side |
| `GET /ce/api/onboarding/stalled/?format=datatables&term_id=&days=7` | Students with no step completion in last N days and not yet completed | datatables server-side |
| `GET /ce/api/onboarding/timeline/?term_id=&highschool_id=` | Array `[{date, started, completed}, …]` across the registration→payment window | plain JSON |
| `GET /ce/api/onboarding/funnel/?term_id=&highschool_id=` | Array `[{step_key, label, completed, not_applicable, pending}, …]` | plain JSON |

- Permission class: reuse `CIS_user_only` (imported from `cis.permissions` — same pattern as `StudentViewSet` at `cis/views/student.py:269`).
- `by_student` builds on `StudentOnboarding` + annotated last step completion; `by_highschool` and `timeline` read from `DailyOnboardingStats` so they're cheap; `stalled` is a live queryset (rare page, cheap enough).

## UI Integration

`cis/templates/cis/students/index.html` changes only:

1. Add nav-tab entries:
   ```django
   {% include "student_onboarding/ce/_tabs_nav.html" %}
   ```
   immediately after the existing `#all` nav-tab `<li>`.

2. Add tab-pane bodies:
   ```django
   {% include "student_onboarding/ce/_tab_by_student.html" %}
   {% include "student_onboarding/ce/_tab_by_highschool.html" %}
   {% include "student_onboarding/ce/_tab_timeline.html" %}
   {% include "student_onboarding/ce/_tab_funnel.html" %}
   {% include "student_onboarding/ce/_tab_stalled.html" %}
   ```
   after the existing `#all` tab-pane `</div>`.

Views `cis/views/student.py:1364` (`index`) will pass two new context vars: `onboarding_timeline_start` and `onboarding_timeline_end`, read via `Setting.get_value(key, 'starting_date')` and `Setting.get_value(key, 'tuition_pay_end_date')` following the pattern already in `cis/settings/registrations.py`. Templates use them to set chart x-axis bounds.

- DataTables init mirrors existing `#records_all` pattern in `index.html:173` (serverSide, stateSave, CSV/Print buttons, 50/page).
- Charts use **Chart.js** (to match the `client_management` repo's stack). Load from CDN in the tab template (`<script src="https://cdn.jsdelivr.net/npm/chart.js">`), kept self-contained so no changes to shared base templates are needed. Timeline = `line` chart; Funnel = horizontal `bar` chart.

## Daily Aggregate Job

New management command `aggregate_onboarding_stats.py` in `student_onboarding/management/commands/`:

```
python manage.py aggregate_onboarding_stats [--date YYYY-MM-DD] [--term <id>] [--backfill-from YYYY-MM-DD]
```

Behavior:
- Defaults to `today` and the active term.
- Idempotent: uses `update_or_create` on `(date, term, highschool, step_key)`.
- For the cohort of `StudentOnboarding` rows in the term:
  - Writes one "all-HS" row: `started_count` = count where `started_on::date <= date`; `completed_count` = count where `completed_on::date <= date`.
  - Writes one row per HS with the same counts scoped to the HS.
  - Writes per-step rows with `step_completed_count` = count of `StudentOnboardingStep.completed_on::date == date` per step key (drives funnel-over-time if we ever want it; funnel endpoint reads latest date).
- `--backfill-from`: loops from that date to today, useful for populating history the first time.

Register as a `CronTab` entry via a new migration or a small `register_cron` invocation in `student_onboarding/apps.py:ready()` (guarded against duplicates) — schedule `0 2 * * *`. Follows pattern in `webapp/cis/management/commands/cron_jobs.py:86`.

## Critical Files

**New (in submodule):**
- `student_onboarding/student_onboarding/views.py`
- `student_onboarding/student_onboarding/urls.py`
- `student_onboarding/student_onboarding/serializers.py`
- `student_onboarding/student_onboarding/migrations/0002_dailyonboardingstats.py`
- `student_onboarding/student_onboarding/management/commands/aggregate_onboarding_stats.py`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tabs_nav.html`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tab_by_student.html`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tab_by_highschool.html`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tab_timeline.html`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tab_funnel.html`
- `student_onboarding/student_onboarding/templates/student_onboarding/ce/_tab_stalled.html`

**Modified:**
- `student_onboarding/student_onboarding/models.py` — add `DailyOnboardingStats`
- `student_onboarding/student_onboarding/admin.py` — register new model
- `student_onboarding/models.py` (outer proxy) — re-export new model
- `webapp/cis/templates/cis/students/index.html` — `{% include %}` the six partials
- `webapp/cis/views/student.py` (`index` at line 1364) — pass timeline date bounds
- `webapp/cis/urls.py` — `path('ce/', include('student_onboarding.urls'))` (or equivalent)

## Reused Utilities

- `student_onboarding.api` (idempotent step mutations) — the aggregate command never mutates steps, just reads; no reuse needed on write side.
- `cis.utils.active_term` — for defaulting term filter server-side.
- `cis.models.settings.Setting.get_value(key, field)` — registration date lookup.
- `cis.permissions.CIS_user_only` — DRF permission.
- DataTables + `rest_framework_datatables` viewset/pagination — already wired project-wide; `StudentViewSet` at `cis/views/student.py:269` is the reference.
- Chart.js — loaded via CDN in the new tab templates (matches chart lib used in the `client_management` repo).
- `CronTab` model (`cis/models/crontab.py`) — registration pattern already used by other submodules.

## Verification

1. **Migrations**
   ```
   docker exec django_web_ewu python webapp/manage.py makemigrations student_onboarding
   docker exec django_web_ewu python webapp/manage.py migrate
   ```

2. **Backfill stats** — so the chart has data on first load:
   ```
   docker exec django_web_ewu python webapp/manage.py aggregate_onboarding_stats --backfill-from 2026-01-01
   ```

3. **Unit tests** — extend `student_onboarding/tests.py`:
   - `DailyOnboardingStatsAggregateTests`: seed fixtures, run command for a specific date, assert counts.
   - Idempotency: run twice, assert no duplicates.
   - Per-HS scoping: create 2 HSes, assert per-HS rows sum to all-HS row.
   ```
   docker exec django_web_ewu python webapp/manage.py test student_onboarding
   ```

4. **Manual UI check**
   - Visit `http://127.0.0.1:8002/ce/students/` as a CE user.
   - Confirm five new tabs render: By Student, By High School, Timeline, Funnel, Stalled.
   - `By Student`: search/sort work, links open student detail.
   - `By High School`: % bars render, sort by % desc shows top performers.
   - `Timeline`: chart x-axis spans `starting_date` → `tuition_pay_end_date`.
   - `Funnel`: bars per step, counts match spot-check in shell.
   - `Stalled`: change `?days=` query, row count adjusts.

5. **Cron registration**
   ```
   docker exec django_web_ewu python webapp/manage.py shell -c "from cis.models import CronTab; print(CronTab.objects.filter(command__contains='aggregate_onboarding_stats'))"
   ```
