import uuid
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import Group
from django.contrib.auth.signals import user_logged_in
from django.test import TestCase, RequestFactory
from django.utils import timezone

from cis.models.customuser import CustomUser
from cis.models.term import AcademicYear, Term
from cis.models.student import Student

from student_onboarding import api, events
from student_onboarding.models import StudentOnboarding, StudentOnboardingStep
from student_onboarding.signals import onboarding_event


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
        _send(events.APPLICATION_STARTED, self.student)
        onboarding = StudentOnboarding.objects.get(student=self.student, term=self.term)
        keys = set(onboarding.steps.values_list('key', flat=True))
        # First-timer (unverified email, no prior data) — verify_info is omitted
        self.assertEqual(keys, {'verify_email', 'ferpa', 'classes', 'student_agreement'})

    def test_ferpa_completed_marks_step_done(self):
        _send(events.APPLICATION_STARTED, self.student)
        _send(events.FERPA_COMPLETED, self.student)
        step = StudentOnboardingStep.objects.get(
            onboarding__student=self.student, key='ferpa'
        )
        self.assertEqual(step.status, 'completed')

    def test_classes_applied_marks_step_done(self):
        _send(events.APPLICATION_STARTED, self.student)
        _send(events.CLASSES_APPLIED, self.student)
        step = StudentOnboardingStep.objects.get(
            onboarding__student=self.student, key='classes'
        )
        self.assertEqual(step.status, 'completed')

    def test_user_logged_in_creates_new_onboarding_for_new_term(self):
        # Seed onboarding for term T2
        _send(events.APPLICATION_STARTED, self.student)
        self.assertEqual(StudentOnboarding.objects.filter(student=self.student).count(), 1)

        # Simulate term rollover by patching active_term to a new term
        new_term = _make_term('T3')
        with patch('student_onboarding.student_onboarding.api.active_term', return_value=new_term), \
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
