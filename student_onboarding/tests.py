import datetime
import json
from urllib.parse import quote
import uuid
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import Group
from django.contrib.auth.signals import user_logged_in
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from cis.models.customuser import CustomUser
from cis.models.term import AcademicYear, Term
from cis.models.student import Student

from student_onboarding import api, events
from student_onboarding.models import StudentOnboarding, StudentOnboardingStep
from student_onboarding.signals import onboarding_event

try:
    from django_login_history.models import post_login as _login_history_post_login
except Exception:
    _login_history_post_login = None


def _send(event, student):
    onboarding_event.send(sender='tests', event=event, student=student)


def _make_user(**overrides):
    defaults = {
        'username': f'u-{uuid.uuid4()}',
        'email': f'{uuid.uuid4()}@example.com',
        'first_name': 'Test',
        'last_name': 'User',
        'psid': '-',
    }
    defaults.update(overrides)
    return CustomUser.objects.create(**defaults)


def _make_term(code='TERM1'):
    ay = AcademicYear.objects.create(name=f'AY-{uuid.uuid4()}')
    return Term.objects.create(academic_year=ay, code=code, label=f'L-{code}')


class OnboardingApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('T1')

    def setUp(self):
        # The Student post_save receiver seeds onboarding at creation
        # (cis.signals.onboarding.seed_on_student_created). These tests are
        # about add_step/complete_step counter arithmetic, so the fixture is
        # built with the receiver's active-term lookup returning None, which
        # is its documented no-op path. The auto-seed itself is covered by
        # cis.tests.test_two_phase_onboarding_seeding.
        with patch('cis.signals.onboarding.active_term', return_value=None):
            self.student = Student.objects.create(user=_make_user())
        active_term_patcher = patch('student_onboarding.student_onboarding.api.active_term', return_value=self.term)
        active_term_patcher.start()
        self.addCleanup(active_term_patcher.stop)

    def test_add_step_creates_and_increments_total(self):
        step = api.add_step(
            self.student, key='ferpa', label='FERPA', url_name='student:ferpa'
        )
        self.assertEqual(step.status, 'pending')
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.total_steps, 1)
        self.assertEqual(onboarding.completed_steps, 0)

    def test_add_step_is_idempotent(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.total_steps, 1)
        self.assertEqual(onboarding.steps.count(), 1)

    def test_complete_step_increments_completed_and_recomputes(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        api.add_step(self.student, key='classes', label='Classes')

        step = api.complete_step(self.student, key='ferpa')
        self.assertEqual(step.status, 'completed')
        self.assertIsNotNone(step.completed_on)

        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.completed_steps, 1)
        self.assertIsNone(onboarding.completed_on)

        api.complete_step(self.student, key='classes')
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.completed_steps, 2)
        self.assertIsNotNone(onboarding.completed_on)

    def test_complete_step_no_op_when_step_missing(self):
        result = api.complete_step(self.student, key='nonexistent')
        self.assertIsNone(result)

    def test_complete_step_does_not_double_count(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        api.complete_step(self.student, key='ferpa')
        api.complete_step(self.student, key='ferpa')
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.completed_steps, 1)

    def test_progress_percent(self):
        api.add_step(self.student, key='a', label='A')
        api.add_step(self.student, key='b', label='B')
        api.add_step(self.student, key='c', label='C')
        api.add_step(self.student, key='d', label='D')
        api.complete_step(self.student, key='a')
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.progress_percent, 25)

    def test_mark_not_applicable_counts_as_done(self):
        api.add_step(self.student, key='verify_info', label='Verify')
        api.mark_not_applicable(self.student, key='verify_info')
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        self.assertEqual(onboarding.completed_steps, 1)
        self.assertIsNotNone(onboarding.completed_on)


class OnboardingSignalReceiverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('T2')

    def setUp(self):
        # See OnboardingApiTests.setUp: suppress the Student post_save
        # receiver during fixture creation so these event-wiring tests exert
        # full control over when seeding happens, via explicit _send() calls.
        with patch('cis.signals.onboarding.active_term', return_value=None):
            self.student = Student.objects.create(user=_make_user())
        active_term_patcher = patch('student_onboarding.student_onboarding.api.active_term', return_value=self.term)
        active_term_patcher.start()
        self.addCleanup(active_term_patcher.stop)
        cis_active_patcher = patch(
            'myce_tenant_configs.services.onboarding_steps.active_term', return_value=self.term
        )
        cis_active_patcher.start()
        self.addCleanup(cis_active_patcher.stop)

    def test_application_started_seeds_default_steps(self):
        # This test is about event-to-step wiring (APPLICATION_STARTED ->
        # seeding), not the seeding gate. An unverified student is now
        # deliberately seeded with verify_email alone (see
        # test_unverified_student_seeded_with_verify_email_only below), so
        # verify the student first to exercise the rest of the catalog.
        self.student.account_verified = True
        self.student.save(update_fields=['account_verified'])
        _send(events.APPLICATION_STARTED, self.student)
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        keys = set(onboarding.steps.values_list('key', flat=True))
        # Verified first-timer (no prior data) — verify_email and verify_info
        # are both omitted
        self.assertEqual(keys, {'ferpa', 'classes', 'student_agreement'})

    def test_ferpa_completed_marks_step_done(self):
        # Covers the FERPA_COMPLETED completion handler, which only applies
        # once the ferpa step exists — i.e. once the student is verified.
        self.student.account_verified = True
        self.student.save(update_fields=['account_verified'])
        _send(events.APPLICATION_STARTED, self.student)
        _send(events.FERPA_COMPLETED, self.student)
        step = StudentOnboardingStep.objects.get(
            onboarding__student=self.student, key='ferpa'
        )
        self.assertEqual(step.status, 'completed')

    def test_classes_applied_marks_step_done(self):
        # Covers the CLASSES_APPLIED completion handler, which only applies
        # once the classes step exists — i.e. once the student is verified.
        self.student.account_verified = True
        self.student.save(update_fields=['account_verified'])
        _send(events.APPLICATION_STARTED, self.student)
        _send(events.CLASSES_APPLIED, self.student)
        step = StudentOnboardingStep.objects.get(
            onboarding__student=self.student, key='classes'
        )
        self.assertEqual(step.status, 'completed')

    def test_unverified_student_seeded_with_verify_email_only(self):
        # Regression guard from this side of the boundary: an unverified
        # student must be seeded with verify_email alone. The gate itself
        # lives in myce_tenant_configs.services.onboarding_steps
        # (seeded_when=_is_verified on ferpa/classes/student_agreement/
        # tuition_assistance) — this pins that the event wiring here
        # actually respects it end to end.
        _send(events.APPLICATION_STARTED, self.student)
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        keys = set(onboarding.steps.values_list('key', flat=True))
        self.assertEqual(keys, {'verify_email'})

    def test_user_logged_in_creates_new_onboarding_for_new_term(self):
        # Seed onboarding for term T2
        _send(events.APPLICATION_STARTED, self.student)
        self.assertEqual(StudentOnboarding.objects.filter(student=self.student).count(), 1)

        # Simulate term rollover by patching active_term to a new term.
        # All three must be patched: as of cis v0.0.21 reseed_on_term_rollover
        # resolves the term ONCE from its own module and threads it down, so
        # patching only the api/tenant paths leaves the receiver on the old
        # term and the rollover is never simulated at all.
        new_term = _make_term('T3')
        with patch('cis.signals.onboarding.active_term', return_value=new_term), \
             patch('student_onboarding.student_onboarding.api.active_term', return_value=new_term), \
             patch('myce_tenant_configs.services.onboarding_steps.active_term', return_value=new_term):
            request = RequestFactory().get('/', HTTP_USER_AGENT='test-agent')
            user_logged_in.send(
                sender=CustomUser, request=request, user=self.student.user
            )

        self.assertEqual(StudentOnboarding.objects.filter(student=self.student).count(), 2)
        new_onboarding = StudentOnboarding.objects.get(student=self.student, term=new_term)
        self.assertGreater(new_onboarding.total_steps, 0)

    def test_profile_verified_marks_verify_info_step(self):
        # Returning student — has prior onboarding
        prior_term = _make_term('T0')
        StudentOnboarding.objects.create(student=self.student, term=prior_term)

        _send(events.APPLICATION_STARTED, self.student)
        # Returning -> verify_info should be present
        self.assertTrue(
            StudentOnboardingStep.objects.filter(
                onboarding__student=self.student,
                onboarding__term=self.term,
                key='verify_info',
            ).exists()
        )

        _send(events.PROFILE_VERIFIED, self.student)
        step = StudentOnboardingStep.objects.get(
            onboarding__student=self.student,
            onboarding__term=self.term,
            key='verify_info',
        )
        self.assertEqual(step.status, 'completed')


class StepDefinitionNotifyActionTests(TestCase):
    def test_accepts_notify_action(self):
        from student_onboarding.step_registry import StepDefinition
        fn = lambda student, term: None
        step = StepDefinition(key='x', label='X', notify_action=fn)
        self.assertIs(step.notify_action, fn)

    def test_notify_action_defaults_none(self):
        from student_onboarding.step_registry import StepDefinition
        self.assertIsNone(StepDefinition(key='x', label='X').notify_action)


class NotifyActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('NA1')

    def setUp(self):
        self.student = Student.objects.create(user=_make_user())
        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)
        p2 = patch('student_onboarding.student_onboarding.management.commands.'
                   'notify_pending_onboarding.active_term', return_value=self.term)
        p2.start(); self.addCleanup(p2.stop)

    def _register_step(self, action):
        from student_onboarding.step_registry import StepDefinition, register, _registry
        register(StepDefinition(key='demo_notify', label='Demo', notify_action=action))
        self.addCleanup(lambda: _registry.pop('demo_notify', None))

    def _config(self, missing_items):
        return {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': missing_items,
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'S',
            'pending_app_email': 'body',
            'add_note': 'No',
        }

    def _run_cmd(self, dry_run, missing_items):
        from student_onboarding.student_onboarding.management.commands.\
            notify_pending_onboarding import Command
        fake_settings = MagicMock()
        fake_settings.from_db.return_value = self._config(missing_items)
        return Command()._run(
            student_regis_pending=fake_settings,
            send_html_mail=MagicMock(),
            dry_run=dry_run,
            only_student=str(self.student.id),
        )

    def test_notify_action_fires_when_step_selected(self):
        action = MagicMock()
        self._register_step(action)
        api.add_step(self.student, key='demo_notify', label='Demo')
        self._run_cmd(dry_run=False, missing_items=['demo_notify'])
        action.assert_called_once_with(self.student, self.term)

    def test_notify_action_not_fired_on_dry_run(self):
        action = MagicMock()
        self._register_step(action)
        api.add_step(self.student, key='demo_notify', label='Demo')
        self._run_cmd(dry_run=True, missing_items=['demo_notify'])
        action.assert_not_called()

    def test_notify_action_not_fired_when_step_not_selected(self):
        # Empty missing_items => generic email still sends, but notify_action
        # must NOT fire on the empty-list default.
        action = MagicMock()
        self._register_step(action)
        api.add_step(self.student, key='demo_notify', label='Demo')
        self._run_cmd(dry_run=False, missing_items=[])
        action.assert_not_called()


class BuildPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('BP1')

    def setUp(self):
        self.student = Student.objects.create(user=_make_user())
        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)

    def _settings_form(self, **overrides):
        config = {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'Finish your onboarding',
            'pending_app_email': 'Hi {{student_first_name}}: {{missing_items}}',
            'add_note': 'No',
        }
        config.update(overrides)
        form = MagicMock()
        form.from_db.return_value = config
        return form

    def _plan(self, **overrides):
        from student_onboarding.student_onboarding import services
        return services.build_plan(
            term=self.term,
            settings_form=self._settings_form(**overrides),
        )

    def test_pending_selected_step_is_sendable(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = self._plan()
        self.assertEqual(len(plan.sendable), 1)
        row = plan.sendable[0]
        self.assertEqual(row.decision, services.DECISION_SEND)
        self.assertEqual(row.missing_items, ['FERPA'])

    def test_all_steps_done_is_excluded(self):
        # DECISION_ALL_DONE is a drift bucket: completed_on is still NULL while
        # no step is pending. Built with queryset.update() so the api helpers'
        # recompute_completion() does not stamp completed_on and lift the row
        # out of build_plan's queryset entirely.
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.steps.update(status=StudentOnboardingStep.STATUS_COMPLETED)
        self.assertIsNone(
            StudentOnboarding.objects.get(pk=onboarding.pk).completed_on)
        plan = self._plan()
        self.assertEqual(plan.sendable, [])
        self.assertEqual(
            plan.ids_with_decision(services.DECISION_ALL_DONE),
            [str(self.student.id)],
        )

    def test_unselected_step_is_no_match(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='classes', label='Register')
        plan = self._plan(missing_items=['ferpa'])
        self.assertEqual(plan.sendable, [])
        self.assertEqual(
            plan.ids_with_decision(services.DECISION_NO_MATCH),
            [str(self.student.id)],
        )

    def test_recently_notified_is_rate_limited(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.last_notified_on = timezone.now()
        onboarding.save(update_fields=['last_notified_on'])
        plan = self._plan()
        self.assertEqual(plan.sendable, [])
        self.assertEqual(
            plan.ids_with_decision(services.DECISION_RATE_LIMITED),
            [str(self.student.id)],
        )

    def test_ignore_rate_limit_makes_it_sendable_again(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.last_notified_on = timezone.now()
        onboarding.save(update_fields=['last_notified_on'])
        plan = services.build_plan(
            term=self.term, settings_form=self._settings_form(),
            ignore_rate_limit=True,
        )
        self.assertEqual(len(plan.sendable), 1)

    def test_legacy_missing_item_alias_still_matches(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = self._plan(missing_items=['missing_ferpa'])
        self.assertEqual(len(plan.sendable), 1)

    def test_debug_mode_redirects_to_notify_address(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = self._plan()
        self.assertTrue(plan.debug_mode)
        self.assertEqual(plan.sendable[0].to_email, ['staff@example.com'])

    def test_body_is_rendered_with_substitutions(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        row = self._plan().sendable[0]
        self.assertIn(self.student.user.first_name, row.body)
        self.assertIn('FERPA', row.body)
        self.assertEqual(row.subject, 'Finish your onboarding')

    def test_inactive_returns_skip_reason(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        self.assertEqual(self._plan(is_active='No'), services.SKIP_INACTIVE)

    def test_force_overrides_inactive(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = services.build_plan(
            term=self.term, settings_form=self._settings_form(is_active='No'),
            force=True,
        )
        self.assertEqual(len(plan.sendable), 1)

    def test_no_active_term_returns_skip_reason(self):
        from student_onboarding.student_onboarding import services
        with patch('student_onboarding.student_onboarding.services.active_term',
                   return_value=None):
            result = services.build_plan(settings_form=self._settings_form())
        self.assertEqual(result, services.SKIP_NO_TERM)

    def test_get_pending_notifications_returns_sendable_rows(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        rows = services.get_pending_notifications(
            term=self.term, settings_form=self._settings_form())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].student.id, self.student.id)

    def test_send_notifications_sends_and_stamps(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = self._plan()
        mailer = MagicMock()
        result = services.send_notifications(
            plan.sendable, config=plan.config, term=self.term,
            send_html_mail=mailer,
        )
        mailer.assert_called_once()
        self.assertEqual(len(result['sent']), 1)
        self.assertEqual(result['by_step'], {'ferpa': 1})
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        self.assertIsNotNone(onboarding.last_notified_on)

    def test_send_notifications_dry_run_does_not_send(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        plan = self._plan()
        mailer = MagicMock()
        services.send_notifications(
            plan.sendable, config=plan.config, term=self.term,
            send_html_mail=mailer, dry_run=True,
        )
        mailer.assert_not_called()
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        self.assertIsNone(onboarding.last_notified_on)


class NotifyCommandSummaryTests(TestCase):
    """The command's summary/log contract is what the cron log shows, so it
    is pinned across the service refactor."""

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('NC1')

    def setUp(self):
        self.student = Student.objects.create(user=_make_user())
        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)
        p2 = patch('student_onboarding.student_onboarding.management.commands.'
                   'notify_pending_onboarding.active_term',
                   return_value=self.term)
        p2.start(); self.addCleanup(p2.stop)

    def _run(self, dry_run=True, **config_overrides):
        from student_onboarding.student_onboarding.management.commands.\
            notify_pending_onboarding import Command
        config = {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'S',
            'pending_app_email': 'body',
            'add_note': 'No',
        }
        config.update(config_overrides)
        form = MagicMock()
        form.from_db.return_value = config
        return Command()._run(
            student_regis_pending=form,
            send_html_mail=MagicMock(),
            dry_run=dry_run,
            only_student=str(self.student.id),
        )

    def test_summary_counts_a_sendable_student(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        summary, log = self._run()
        self.assertIn('[dry-run] Sent 1', summary)
        self.assertIn('By step: 1 ', summary)
        self.assertEqual(len(log['sent']), 1)
        self.assertEqual(log['sent'][0]['issues'], ['FERPA'])
        self.assertEqual(log['by_step'], {'ferpa': 1})

    def test_log_buckets_are_present_and_named_as_before(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        _, log = self._run()
        self.assertEqual(
            sorted(log.keys()),
            ['by_step', 'sent', 'skipped_all_done', 'skipped_no_match',
             'skipped_rate_limit'],
        )

    def test_unselected_step_lands_in_skipped_no_match(self):
        api.add_step(self.student, key='classes', label='Register')
        _, log = self._run(missing_items=['ferpa'])
        self.assertEqual(log['skipped_no_match'], [str(self.student.id)])
        self.assertEqual(log['sent'], [])

    def test_inactive_returns_legacy_message(self):
        summary, log = self._run(is_active='No')
        self.assertEqual(summary, 'Notification disabled (is_active=No). Skipped.')
        self.assertEqual(log, {})

    def test_skipped_no_match_ids_are_in_queryset_iteration_order(self):
        # skipped_no_match merges two decisions (DECISION_NO_MATCH and
        # DECISION_NO_EMAIL). The pre-refactor command appended ids to this
        # bucket inline as it walked the queryset, interleaving the two
        # reasons in iteration order -- it must NOT come out grouped by
        # reason (i.e. not `ids_with_decision(A) + ids_with_decision(B)`).
        #
        # self.student (created first, in setUp) is put in the NO_EMAIL
        # bucket, and a second student (created after) is put in the
        # NO_MATCH bucket, so that queryset-iteration order (first student,
        # then second) disagrees with decision-grouped order (NO_MATCH
        # emitted before NO_EMAIL by the command's own DECISION_NO_MATCH,
        # DECISION_NO_EMAIL argument order) -- a concatenation would emit
        # [second, self.student] where the real walk order is
        # [self.student, second].
        from student_onboarding.student_onboarding import services
        from student_onboarding.student_onboarding.management.commands.\
            notify_pending_onboarding import Command

        self.student.user.email = ''
        self.student.user.save(update_fields=['email'])
        api.add_step(self.student, key='ferpa', label='FERPA')

        second_student = Student.objects.create(user=_make_user())
        api.add_step(second_student, key='classes', label='Register')

        config = {
            'is_active': 'Yes',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'S',
            'pending_app_email': 'body',
            'add_note': 'No',
        }
        form = MagicMock()
        form.from_db.return_value = config

        with patch('django.conf.settings.DEBUG', False):
            plan = services.build_plan(term=self.term, settings_form=form)
            expected_order = [
                str(row.student.id) for row in plan.rows
                if row.decision in (services.DECISION_NO_MATCH,
                                    services.DECISION_NO_EMAIL)
            ]
            self.assertEqual(sorted(expected_order),
                             sorted([str(self.student.id),
                                     str(second_student.id)]))

            _, log = Command()._run(
                student_regis_pending=form,
                send_html_mail=MagicMock(),
                dry_run=True,
                only_student=None,
            )

        self.assertEqual(log['skipped_no_match'], expected_order)

    def test_no_active_term_returns_legacy_message(self):
        # services.build_plan re-derives `term` via its own `active_term`
        # import when the command passes it an explicit None (the "no
        # active term" case), so both call sites need to agree here. In
        # production they're the same unmocked function, so this only
        # matters under test.
        with patch('student_onboarding.student_onboarding.management.commands.'
                   'notify_pending_onboarding.active_term', return_value=None), \
             patch('student_onboarding.student_onboarding.services.active_term',
                   return_value=None):
            summary, log = self._run()
        self.assertEqual(summary, 'No active term. Skipped.')


class PendingNotificationDetailViewTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if _login_history_post_login is not None:
            user_logged_in.disconnect(_login_history_post_login)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if _login_history_post_login is not None:
            user_logged_in.connect(_login_history_post_login)

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        Group.objects.get_or_create(name='ce')
        cls.term = _make_term('PD1')

    def setUp(self):
        # See OnboardingApiTests.setUp: suppress the Student post_save
        # receiver during fixture creation. test_no_onboarding_record_renders_
        # without_a_row depends on a student with genuinely no onboarding
        # record, and the other tests here build their own record explicitly
        # via api.add_step, so the auto-seed would only be noise.
        with patch('cis.signals.onboarding.active_term', return_value=None):
            self.student = Student.objects.create(user=_make_user())
        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)
        p2 = patch('student_onboarding.student_onboarding.views.active_term',
                   return_value=self.term)
        p2.start(); self.addCleanup(p2.stop)

        self.staff = _make_user(email=f'ce-{uuid.uuid4()}@example.com')
        self.staff.groups.add(Group.objects.get(name='ce'))
        self.client.force_login(self.staff)

        self.config = {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'Finish up',
            'pending_app_email': 'Hi {{student_first_name}}: {{missing_items}}',
            'add_note': 'No',
        }
        p3 = patch('student_onboarding.student_onboarding.services._load_config',
                   side_effect=lambda settings_form=None: self.config)
        p3.start(); self.addCleanup(p3.stop)

    def _url(self):
        return reverse('student_onboarding_ce:pending_notification_detail',
                       args=[self.student.id])

    def test_shows_rendered_email_for_a_sendable_student(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['row'].decision, services.DECISION_SEND)
        self.assertContains(response, 'Finish up')
        self.assertContains(response, 'FERPA')

    def test_explains_why_a_rate_limited_student_is_excluded(self):
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.last_notified_on = timezone.now()
        onboarding.save(update_fields=['last_notified_on'])
        response = self.client.get(self._url())
        self.assertEqual(response.context['row'].decision,
                         services.DECISION_RATE_LIMITED)
        self.assertIsNotNone(response.context['row'].next_eligible_on)

    def test_rate_limited_student_still_shows_content_and_recipients(self):
        """A rate-limited student is the common case, not an edge case -
        the operator must be able to see exactly what Send Now would mail
        before clicking it, even though the row is not sendable."""
        from student_onboarding.student_onboarding import services
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.last_notified_on = timezone.now()
        onboarding.save(update_fields=['last_notified_on'])

        response = self.client.get(self._url())
        row = response.context['row']
        self.assertEqual(row.decision, services.DECISION_RATE_LIMITED)
        self.assertIsNotNone(row.next_eligible_on)
        self.assertEqual(row.subject, 'Finish up')
        self.assertTrue(row.to_email)
        self.assertContains(response, 'Finish up')
        self.assertContains(response, row.to_email[0])

    def test_no_onboarding_record_renders_without_a_row(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['row'])

    def test_send_now_sends_and_stamps_even_when_rate_limited(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.last_notified_on = timezone.now() - datetime.timedelta(hours=1)
        onboarding.save(update_fields=['last_notified_on'])
        before = onboarding.last_notified_on

        with patch('student_onboarding.student_onboarding.services'
                   '.send_notifications') as mock_send:
            mock_send.return_value = {'sent': [{'student_id': str(self.student.id)}],
                                      'by_step': {'ferpa': 1}}
            response = self.client.post(self._url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_send.call_count, 1)
        rows_sent = mock_send.call_args.args[0]
        self.assertEqual(len(rows_sent), 1)
        self.assertEqual(str(rows_sent[0].student.id), str(self.student.id))
        self.assertEqual(before, StudentOnboarding.objects.get(
            student=self.student, term=self.term).last_notified_on)

    def test_send_now_really_sends_when_not_mocked(self):
        api.add_step(self.student, key='ferpa', label='FERPA')
        with patch('student_onboarding.student_onboarding.services'
                   '.send_html_mail', create=True):
            with patch('mailer.send_html_mail') as mailer:
                self.client.post(self._url(), follow=True)
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        self.assertIsNotNone(onboarding.last_notified_on)
        mailer.assert_called_once()

    def test_send_now_does_nothing_when_student_has_no_pending_items(self):
        with patch('student_onboarding.student_onboarding.services'
                   '.send_notifications') as mock_send:
            self.client.post(self._url(), follow=True)
        mock_send.assert_not_called()

    def test_anonymous_is_redirected(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    def test_post_with_term_id_in_query_string_acts_on_that_term(self):
        """The detail template's <form method="post"> has no `action`, so a
        term_id carried on the page URL must survive the POST and drive
        which term's plan gets built and sent."""
        from student_onboarding.student_onboarding import services
        other_term = _make_term('PD2')
        with patch('student_onboarding.student_onboarding.services'
                   '.build_plan', wraps=services.build_plan) as mock_build:
            self.client.post(f'{self._url()}?term_id={other_term.id}')
        # Every call the POST handler makes to build_plan (including the
        # rate-limited redisplay path, if taken) must resolve to other_term,
        # not the active term the view would fall back to without a term_id.
        terms_used = {call.kwargs.get('term') for call in mock_build.call_args_list}
        self.assertEqual(terms_used, {other_term})


    def test_detail_response_is_not_frame_denied(self):
        """The detail page renders inside the list page's shared #details_src
        iframe, so it must opt out of the project's X_FRAME_OPTIONS = DENY -
        as every other modal target here does (drop_wd, pd_event,
        support_ticket, tech_center_staff, highschool_admin, future_sections).
        Without it the browser refuses the frame and the user sees "refused to
        connect" instead of the preview.

        Asserted on the response header, not on the view function:
        xframe_options_exempt flags the *response*, which
        XFrameOptionsMiddleware then honors, so only the header proves it.
        """
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get('X-Frame-Options'))

    def test_list_page_is_still_frame_denied(self):
        # The list page is top-level, never framed - it keeps the
        # project-wide clickjacking protection.
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertEqual(response.headers.get('X-Frame-Options'), 'DENY')

    def test_back_url_is_rendered_when_supplied(self):
        back = reverse('cis:student', args=[self.student.id])
        response = self.client.get(self._url() + '?back=' + back)
        self.assertEqual(response.context['back_url'], back)
        self.assertContains(response, 'Back to Student Record')

    def test_no_back_button_without_the_param(self):
        # Reached from the list page's details modal - nothing to go back to.
        response = self.client.get(self._url())
        self.assertEqual(response.context['back_url'], '')
        self.assertNotContains(response, 'Back to Student Record')

    def test_offsite_back_url_is_rejected(self):
        # back is rendered as an href; an unchecked value would be an open
        # redirect out of the application.
        response = self.client.get(self._url() + '?back=https://evil.example.com/x')
        self.assertEqual(response.context['back_url'], '')

    def test_send_now_redirect_preserves_the_query_string(self):
        back = reverse('cis:student', args=[self.student.id])
        response = self.client.post(self._url() + '?back=' + back, follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('back=', response['Location'])

class PendingNotificationsViewTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if _login_history_post_login is not None:
            user_logged_in.disconnect(_login_history_post_login)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if _login_history_post_login is not None:
            user_logged_in.connect(_login_history_post_login)

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        Group.objects.get_or_create(name='ce')
        cls.term = _make_term('PV1')

    def setUp(self):
        self.student = Student.objects.create(user=_make_user())
        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)
        p2 = patch('student_onboarding.student_onboarding.views.active_term',
                   return_value=self.term)
        p2.start(); self.addCleanup(p2.stop)

        self.staff = _make_user(email=f'ce-{uuid.uuid4()}@example.com')
        self.staff.set_password('pw12345!')
        self.staff.save()
        self.staff.groups.add(Group.objects.get(name='ce'))
        self.client.force_login(self.staff)

    def _patch_plan(self, **overrides):
        config = {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'S',
            'pending_app_email': 'body {{missing_items}}',
            'add_note': 'No',
        }
        config.update(overrides)
        form = MagicMock()
        form.from_db.return_value = config
        p = patch('student_onboarding.student_onboarding.services._load_config',
                  return_value=config)
        p.start(); self.addCleanup(p.stop)

    def test_anonymous_is_redirected(self):
        self.client.logout()
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertEqual(response.status_code, 302)

    def test_sendable_student_is_listed(self):
        self._patch_plan()
        api.add_step(self.student, key='ferpa', label='FERPA')
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rows']), 1)
        self.assertContains(response, 'FERPA')

    def test_excluded_student_is_not_listed(self):
        self._patch_plan(missing_items=['ferpa'])
        api.add_step(self.student, key='classes', label='Register')
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertEqual(response.context['rows'], [])

    def test_skip_reason_is_shown_when_disabled(self):
        self._patch_plan(is_active='No')
        api.add_step(self.student, key='ferpa', label='FERPA')
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertIsNotNone(response.context['skip_reason'])
        self.assertEqual(response.context['rows'], [])

    def test_view_details_link_carries_the_selected_term_id(self):
        self._patch_plan()
        api.add_step(self.student, key='ferpa', label='FERPA')
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'),
            {'term_id': str(self.term.id)})
        detail_url = reverse(
            'student_onboarding_ce:pending_notification_detail',
            args=[self.student.id])
        self.assertContains(
            response, f'{detail_url}?term_id={self.term.id}')

    def test_wording_states_whatif_for_a_non_active_term(self):
        self._patch_plan()
        other_term = _make_term('PV2')
        api.add_step(self.student, key='ferpa', label='FERPA')
        onboarding = StudentOnboarding.objects.get(
            student=self.student, term=self.term)
        onboarding.term = other_term
        onboarding.save(update_fields=['term'])
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'),
            {'term_id': str(other_term.id)})
        self.assertFalse(response.context['is_active_term'])
        self.assertContains(response, 'what-if')
        self.assertNotContains(response, 'would be emailed at the')


class PendingOnboardingActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name='student')
        cls.term = _make_term('PA1')

    def test_action_is_registered_for_the_detail_scope(self):
        from myce.component_registry.student import student_actions
        detail = student_actions.for_scope('detail')
        slugs = [slug for group in detail.values()
                 for slug in group['actions'].keys()]
        self.assertIn('pending_onboarding_preview', slugs)

    def test_action_returns_a_same_tab_redirect_with_a_back_param(self):
        # 'redirect', not 'open': this is a page staff read and come back
        # from, so it navigates in place and carries a back link, unlike
        # cis's download_student_pdf which belongs in its own tab.
        from student_onboarding.student_onboarding.actions import (
            pending_onboarding_preview,
        )
        student = Student.objects.create(user=_make_user())
        request = RequestFactory().post('/', {'ids[]': [str(student.id)]})
        payload = json.loads(pending_onboarding_preview(request).content)

        self.assertEqual(payload['outcome'], 'redirect')
        self.assertIn(
            reverse('student_onboarding_ce:pending_notification_detail',
                    args=[student.id]),
            payload['url'],
        )
        self.assertIn(
            'back=' + quote(reverse('cis:student', args=[student.id]), safe=''),
            payload['url'],
        )

    def test_action_errors_when_nothing_is_selected(self):
        from student_onboarding.student_onboarding.actions import (
            pending_onboarding_preview,
        )
        request = RequestFactory().post('/', {})
        payload = json.loads(pending_onboarding_preview(request).content)
        self.assertEqual(payload['outcome'], 'alert')
        self.assertEqual(payload['status'], 'error')


class PendingNotificationCampusGateTests(TestCase):
    """A ce user scoped to campuses they don't process must not be able to
    view or send an onboarding reminder for a verified student who applied
    at a campus outside their scope - typing the detail URL directly must
    404 just like every other out-of-scope student record, and the list
    page must silently drop such rows."""

    @classmethod
    def setUpClass(cls):
        if _login_history_post_login is not None:
            user_logged_in.disconnect(_login_history_post_login)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if _login_history_post_login is not None:
            user_logged_in.connect(_login_history_post_login)

    @classmethod
    def setUpTestData(cls):
        from django.conf import settings as dj_settings

        from cis.models.course import Campus, Cohort, Course
        from cis.models.section import ClassSection, StudentRegistration

        Group.objects.get_or_create(name='student')
        Group.objects.get_or_create(name='ce')
        # cis.signals.registrations.update_registration writes a note as
        # this system user on every StudentRegistration save.
        CustomUser.objects.get_or_create(
            username='cron', defaults={'email': 'cron@example.com'})
        cls.term = _make_term('PC1')

        cls.campus_a = Campus.objects.create(
            name=f'Alpha-{uuid.uuid4().hex[:8]}',
            code=f'{dj_settings.CAMPUS_CODE_PREFIX}-{uuid.uuid4().hex[:8]}')
        cls.campus_b = Campus.objects.create(
            name=f'Beta-{uuid.uuid4().hex[:8]}',
            code=f'{dj_settings.CAMPUS_CODE_PREFIX}-{uuid.uuid4().hex[:8]}')
        cohort = Cohort.objects.create(
            name=f'Co-{uuid.uuid4().hex[:8]}', designator='CO')
        course = Course.objects.create(
            catalog_number='101', title='Intro',
            cohort=cohort, campus=cls.campus_b)
        cls.section = ClassSection.objects.create(
            class_number='1001', term=cls.term, course=course)

    def setUp(self):
        from cis.models.section import StudentRegistration

        # Verified student who applied at campus_b - outside self.ce's scope.
        self.student = Student.objects.create(
            user=_make_user(), account_verified=True)
        StudentRegistration.objects.create(
            student=self.student, class_section=self.section,
            status='applied', verification_status='pending', grade='A',
            status_changed_on={'applied_on': '01/01/2024'},
        )
        api.add_step(self.student, key='ferpa', label='FERPA')

        p = patch('student_onboarding.student_onboarding.api.active_term',
                  return_value=self.term)
        p.start(); self.addCleanup(p.stop)
        p2 = patch('student_onboarding.student_onboarding.views.active_term',
                   return_value=self.term)
        p2.start(); self.addCleanup(p2.stop)

        self.config = {
            'is_active': 'Debug',
            'freq': '3',
            'missing_items': ['ferpa'],
            'notify_address': 'staff@example.com',
            'pending_app_email_subject': 'Finish up',
            'pending_app_email': 'Hi {{student_first_name}}: {{missing_items}}',
            'add_note': 'No',
        }
        p3 = patch('student_onboarding.student_onboarding.services._load_config',
                   side_effect=lambda settings_form=None: self.config)
        p3.start(); self.addCleanup(p3.stop)

        # ce user scoped only to campus_a - cannot process campus_b.
        self.ce = _make_user(email=f'ce-{uuid.uuid4()}@example.com')
        self.ce.groups.add(Group.objects.get(name='ce'))
        self.ce.campus = {'process_campus': [str(self.campus_a.id)],
                          'default_campus': ''}
        self.ce.save()
        self.client.force_login(self.ce)

    def _detail_url(self):
        return reverse('student_onboarding_ce:pending_notification_detail',
                       args=[self.student.id])

    def test_get_detail_404s_for_out_of_scope_student(self):
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 404)

    def test_post_detail_404s_and_sends_nothing_for_out_of_scope_student(self):
        with patch('student_onboarding.student_onboarding.services'
                   '.send_notifications') as mock_send:
            response = self.client.post(self._detail_url())
        self.assertEqual(response.status_code, 404)
        mock_send.assert_not_called()

    def test_out_of_scope_student_is_not_listed(self):
        response = self.client.get(
            reverse('student_onboarding_ce:pending_notifications'))
        self.assertEqual(response.status_code, 200)
        student_ids = [str(row.student.id) for row in response.context['rows']]
        self.assertNotIn(str(self.student.id), student_ids)


class MenuMigrationTests(TestCase):
    """Exercises the migration's functions directly against the live Setting
    row — running the migration itself is not repeatable inside a test."""

    def _setting_model(self):
        from django.apps import apps as django_apps
        return django_apps.get_model('cis', 'Setting')

    def _menu_module(self):
        import importlib
        return importlib.import_module(
            'student_onboarding.student_onboarding.migrations'
            '.0003_menu_pending_onboarding')

    def _fake_apps(self):
        model = self._setting_model()

        class FakeApps:
            def get_model(self, app_label, model_name):
                return model

        return FakeApps()

    def _write_menu(self, items):
        model = self._setting_model()
        value = {'ce_menu': json.dumps(items)}
        # `value` (JSONField) is NOT NULL at the DB layer, so plain
        # get_or_create() (which would insert with value=None first) isn't
        # safe here — pass it via defaults, and update on the found path.
        setting, created = model.objects.get_or_create(
            key='cis.settings.menu', defaults={'value': value})
        if not created:
            setting.value = value
            setting.save()
        return setting

    def _read_menu(self):
        model = self._setting_model()
        setting = model.objects.get(key='cis.settings.menu')
        return json.loads(setting.value['ce_menu'])

    def test_adds_item_to_the_students_group(self):
        module = self._menu_module()
        self._write_menu([{'name': 'students', 'label': 'Students',
                           'sub_menu': [{'name': 'notes', 'label': 'Notes'}]}])
        module.add_menu_item(self._fake_apps(), None)
        sub = self._read_menu()[0]['sub_menu']
        names = [s['name'] for s in sub]
        self.assertIn('pending_onboarding_notifications', names)
        self.assertEqual(names[-1], 'pending_onboarding_notifications')

    def test_is_idempotent(self):
        module = self._menu_module()
        self._write_menu([{'name': 'students', 'sub_menu': []}])
        module.add_menu_item(self._fake_apps(), None)
        module.add_menu_item(self._fake_apps(), None)
        sub = self._read_menu()[0]['sub_menu']
        self.assertEqual(len(sub), 1)

    def test_noops_when_students_group_is_absent(self):
        module = self._menu_module()
        self._write_menu([{'name': 'classes', 'sub_menu': []}])
        module.add_menu_item(self._fake_apps(), None)
        self.assertEqual(self._read_menu()[0]['sub_menu'], [])

    def test_noops_when_setting_row_is_missing(self):
        module = self._menu_module()
        self._setting_model().objects.filter(key='cis.settings.menu').delete()
        module.add_menu_item(self._fake_apps(), None)  # must not raise

    def test_reverse_removes_the_item(self):
        module = self._menu_module()
        self._write_menu([{'name': 'students', 'sub_menu': []}])
        module.add_menu_item(self._fake_apps(), None)
        module.remove_menu_item(self._fake_apps(), None)
        self.assertEqual(self._read_menu()[0]['sub_menu'], [])


