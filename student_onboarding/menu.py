"""Student onboarding sidebar item (cis.menu_registry).

Migration 0003 appended it to ce `students`, after `notes` (70).
On a cis without the registry nothing registers.
"""
try:
    from cis.menu_registry import menu
except ImportError:   # cis without the registry: the menu migrations still apply
    menu = None

if menu is not None:
    menu.register('ce', 'pending_onboarding_notifications', 'Onboarding Reminders',
                  url='student_onboarding_ce:pending_notifications',
                  parent='students', order=75)
