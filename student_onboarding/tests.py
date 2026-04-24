import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.contrib.auth.signals import user_logged_in
from django.test import TestCase, RequestFactory

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
            'cis.signals.onboarding.active_term', return_value=self.term
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
             patch('cis.signals.onboarding.active_term', return_value=new_term):
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
