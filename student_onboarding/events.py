"""
Event name constants for the built-in onboarding lifecycle.
Producers should import these instead of passing bare strings.
Other apps define their own constants for events they dispatch
(e.g. studenttransactions.events.PAYMENT_MADE).
"""
APPLICATION_STARTED = 'application_started'
FERPA_COMPLETED = 'ferpa_completed'
CLASSES_APPLIED = 'classes_applied'
PROFILE_VERIFIED = 'profile_verified'
EMAIL_VERIFIED = 'email_verified'
STUDENT_AGREEMENT_SIGNED = 'student_agreement_signed'
TUITION_ASSISTANCE_SUBMITTED = 'tuition_assistance_submitted'
