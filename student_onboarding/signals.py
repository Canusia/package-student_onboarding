"""
Single generic lifecycle signal.

Producers dispatch with an `event` name (string) and the target `student`.
Consumers register via `onboarding.handlers.register(event_name, fn)`
from their AppConfig.ready(). A bridge receiver in OnboardingConfig.ready()
fans out to registered handlers.

Rationale for one signal + registry (vs. one Django signal per event): new
events only need a string constant — no signal plumbing. Extensions from
other apps (e.g. studenttransactions dispatching 'payment_made') require
no changes in the onboarding app.
"""
from django.dispatch import Signal

onboarding_event = Signal()  # kwargs: event (str), student
