"""
String-keyed handler registry for onboarding events.

Apps register handlers from their AppConfig.ready() via
`onboarding.handlers.register(event_name, fn)`. A bridge receiver wired
in OnboardingConfig.ready() listens to the generic `onboarding_event`
Django signal and fans out to the registered handlers by event name.

Handlers are called as fn(student=..., **extra_kwargs). An unknown event
is a no-op — there is no "unknown handler" warning, so typos fail
quietly. Prefer `onboarding.events.X` or an app-local events module over
bare strings.
"""
from collections import defaultdict

_handlers = defaultdict(list)


def register(event, handler):
    """Register `handler` to run whenever `event` is dispatched.
    Multiple handlers per event are allowed; they run in registration order."""
    if handler not in _handlers[event]:
        _handlers[event].append(handler)


def unregister(event, handler):
    """Remove a previously registered handler. No-op if not registered."""
    try:
        _handlers[event].remove(handler)
    except ValueError:
        pass


def dispatch(event, student, **kwargs):
    """Invoke every handler registered for `event`."""
    for handler in _handlers.get(event, []):
        handler(student=student, **kwargs)


def registered_events():
    """Introspection: return a dict of {event: [handler, ...]}."""
    return {event: list(fns) for event, fns in _handlers.items() if fns}
