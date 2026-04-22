from . import api


def onboarding_progress(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}
    student = getattr(user, 'student', None)
    if student is None:
        return {}
    return {'onboarding': api.get_or_create_for_current_term(student)}
