from django.db import models
from django.utils import timezone


class StudentOnboarding(models.Model):
    student = models.ForeignKey(
        'cis.Student', on_delete=models.CASCADE, related_name='onboardings'
    )
    term = models.ForeignKey('cis.Term', on_delete=models.PROTECT)
    started_on = models.DateTimeField(auto_now_add=True)
    completed_on = models.DateTimeField(null=True, blank=True)

    total_steps = models.PositiveSmallIntegerField(default=0)
    completed_steps = models.PositiveSmallIntegerField(default=0)

    last_notified_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('student', 'term')]
        indexes = [models.Index(fields=['student', 'term'])]

    def __str__(self):
        return f'Onboarding({self.student_id}, {self.term_id})'

    @property
    def progress_percent(self):
        if not self.total_steps:
            return 0
        return int(100 * self.completed_steps / self.total_steps)

    def recompute_completion(self):
        if self.total_steps and self.completed_steps >= self.total_steps:
            if not self.completed_on:
                self.completed_on = timezone.now()
                self.save(update_fields=['completed_on'])
        elif self.completed_on:
            self.completed_on = None
            self.save(update_fields=['completed_on'])


class StudentOnboardingStep(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_NOT_APPLICABLE = 'not_applicable'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_NOT_APPLICABLE, 'Not applicable'),
    ]

    onboarding = models.ForeignKey(
        StudentOnboarding, on_delete=models.CASCADE, related_name='steps'
    )
    key = models.CharField(max_length=64)
    label = models.CharField(max_length=128)
    url_name = models.CharField(max_length=128, blank=True)
    message = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    order = models.PositiveSmallIntegerField(default=100)
    completed_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('onboarding', 'key')]
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.key} ({self.status})'

    @property
    def is_done(self):
        return self.status in (self.STATUS_COMPLETED, self.STATUS_NOT_APPLICABLE)


class DailyOnboardingStats(models.Model):
    date = models.DateField()
    term = models.ForeignKey('cis.Term', on_delete=models.PROTECT)
    highschool = models.ForeignKey(
        'cis.HighSchool', on_delete=models.PROTECT, null=True, blank=True,
    )
    step_key = models.CharField(max_length=64, blank=True, default='')

    total_onboardings = models.PositiveIntegerField(default=0)
    started_count = models.PositiveIntegerField(default=0)
    completed_count = models.PositiveIntegerField(default=0)
    step_completed_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('date', 'term', 'highschool', 'step_key')]
        indexes = [
            models.Index(fields=['date', 'term']),
            models.Index(fields=['term', 'highschool']),
        ]

    def __str__(self):
        return f'Stats({self.date}, term={self.term_id}, hs={self.highschool_id}, step={self.step_key!r})'
