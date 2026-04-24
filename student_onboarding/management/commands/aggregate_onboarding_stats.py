"""
Daily rollup of onboarding progress counts into DailyOnboardingStats.

Idempotent per (date, term, highschool, step_key) via update_or_create.
Supports backfilling a range of dates.

Usage:
    python manage.py aggregate_onboarding_stats [--date YYYY-MM-DD]
        [--term <id>] [--backfill-from YYYY-MM-DD]
"""
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from cis.utils import active_term

from ...models import (
    StudentOnboarding,
    StudentOnboardingStep,
    DailyOnboardingStats,
)


class Command(BaseCommand):
    help = 'Aggregate daily onboarding completion counts for reporting.'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, default=None,
                            help='ISO date to aggregate. Defaults to today.')
        parser.add_argument('--term', type=str, default=None,
                            help='Term id. Defaults to the active term.')
        parser.add_argument('--backfill-from', type=str, default=None,
                            help='ISO date. Loop from here up to --date/today.')
        parser.add_argument('-t', '--time', type=str, default=None,
                            help='Scheduled run time (passed by cron_jobs runner, used for telemetry).')

    def handle(self, *args, **opts):
        from cis.models.term import Term
        from cis.signals.crontab import cron_task_started, cron_task_done

        scheduled_time = opts.get('time')
        if scheduled_time:
            cron_task_started.send(
                sender=self.__class__, task=self.__class__,
                scheduled_time=scheduled_time,
            )

        if opts['term']:
            term = Term.objects.get(id=opts['term'])
        else:
            term = active_term()
        if term is None:
            self.stderr.write(self.style.ERROR('No active term — aborting.'))
            if scheduled_time:
                cron_task_done.send(
                    sender=self.__class__, task=self.__class__,
                    scheduled_time=scheduled_time,
                    summary='No active term', detailed_log='{}',
                )
            return

        end_date = datetime.date.fromisoformat(opts['date']) if opts['date'] else datetime.date.today()
        if opts['backfill_from']:
            start_date = datetime.date.fromisoformat(opts['backfill_from'])
        else:
            start_date = end_date

        cur = start_date
        while cur <= end_date:
            self._aggregate_for(term, cur)
            cur += datetime.timedelta(days=1)

        summary = f'Aggregated {(end_date - start_date).days + 1} day(s) for term {term.code}.'
        self.stdout.write(self.style.SUCCESS(summary))

        if scheduled_time:
            cron_task_done.send(
                sender=self.__class__, task=self.__class__,
                scheduled_time=scheduled_time,
                summary=summary,
                detailed_log='{}',
            )

    def _aggregate_for(self, term, date):
        day_end = timezone.make_aware(datetime.datetime.combine(date, datetime.time.max))

        onboardings = StudentOnboarding.objects.filter(
            term=term, started_on__lte=day_end,
        )

        total = onboardings.count()
        started = total
        completed = onboardings.filter(completed_on__lte=day_end).count()

        DailyOnboardingStats.objects.update_or_create(
            date=date, term=term, highschool=None, step_key='',
            defaults={
                'total_onboardings': total,
                'started_count': started,
                'completed_count': completed,
                'step_completed_count': 0,
            },
        )

        # Per-HS rollup.
        per_hs = (
            onboardings
            .values('student__highschool_id')
            .annotate(
                total=Count('id'),
                completed=Count('id', filter=Q(completed_on__lte=day_end)),
            )
        )
        for row in per_hs:
            hs_id = row['student__highschool_id']
            if hs_id is None:
                continue
            DailyOnboardingStats.objects.update_or_create(
                date=date, term=term, highschool_id=hs_id, step_key='',
                defaults={
                    'total_onboardings': row['total'],
                    'started_count': row['total'],
                    'completed_count': row['completed'],
                    'step_completed_count': 0,
                },
            )

        # Per-step-key rollup (rollup of completions for that step up to `date`).
        step_rows = (
            StudentOnboardingStep.objects
            .filter(onboarding__term=term, completed_on__lte=day_end)
            .values('key')
            .annotate(completed=Count('id'))
        )
        for row in step_rows:
            DailyOnboardingStats.objects.update_or_create(
                date=date, term=term, highschool=None, step_key=row['key'],
                defaults={
                    'total_onboardings': total,
                    'started_count': 0,
                    'completed_count': 0,
                    'step_completed_count': row['completed'],
                },
            )
