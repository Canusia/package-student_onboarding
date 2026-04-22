# Outer package marker. Outer-level proxy modules (api.py, events.py, …)
# re-export from the inner `student_onboarding.student_onboarding` app so
# consumers can write `from student_onboarding.api import add_step`
# regardless of whether this package is pip-installed (inner is the
# package itself) or used as a submodule (this outer dir wraps it).
