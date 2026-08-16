"""Add the "Onboarding Reminders" sub-item to the CE sidebar's Students group.

The portal menus are pulled (by ``cis.menu.draw_menu``) from the DB-backed
``cis.settings.menu`` Setting, keyed ``<role>_menu`` — each one a JSON *string*.
Editing the hardcoded lists in ``cis/menu.py`` does nothing; the DB row is the
source of truth. This data migration makes the pending-reminder preview page
reachable without hand-editing JSON in the settings UI, and without touching a
``cis`` template.

Only ``ce_menu`` is touched: the page is CE-role gated in this app's urls.py,
so no other role's menu should advertise it.

If the Students group (``name == 'students'``) is absent, this no-ops rather
than creating it — a tenant without that group would otherwise get an orphan
group holding only this one item.

Idempotent: re-running makes no further changes. No-ops if the menu Setting row
doesn't exist yet (e.g. before ``register_settings`` has run on a fresh
install). Depends on cis ``__first__`` so the ``Setting`` model is present
without pinning a tenant-specific cis migration number.
"""
import json

from django.db import migrations

MENU_SETTING_KEY = 'cis.settings.menu'

STUDENTS_GROUP_NAME = 'students'

MENU_ITEM = {
    'label': 'Onboarding Reminders',
    'name': 'pending_onboarding_notifications',
    'url': 'student_onboarding_ce:pending_notifications',
}


def _load_ce_menu(value):
    raw = value.get('ce_menu')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _students_group(items):
    for item in items:
        if item.get('name') == STUDENTS_GROUP_NAME:
            return item
    return None


def add_menu_item(apps, schema_editor):
    Setting = apps.get_model('cis', 'Setting')
    try:
        setting = Setting.objects.get(key=MENU_SETTING_KEY)
    except Setting.DoesNotExist:
        return

    value = setting.value or {}
    items = _load_ce_menu(value)
    if items is None:
        return

    group = _students_group(items)
    if group is None:
        return

    sub_menu = group.setdefault('sub_menu', [])
    if any(sub.get('name') == MENU_ITEM['name'] for sub in sub_menu):
        return

    sub_menu.append(dict(MENU_ITEM))

    value['ce_menu'] = json.dumps(items)
    setting.value = value
    setting.save()


def remove_menu_item(apps, schema_editor):
    Setting = apps.get_model('cis', 'Setting')
    try:
        setting = Setting.objects.get(key=MENU_SETTING_KEY)
    except Setting.DoesNotExist:
        return

    value = setting.value or {}
    items = _load_ce_menu(value)
    if items is None:
        return

    group = _students_group(items)
    if group is None:
        return

    sub_menu = group.get('sub_menu', [])
    pruned = [sub for sub in sub_menu if sub.get('name') != MENU_ITEM['name']]
    if len(pruned) == len(sub_menu):
        return

    group['sub_menu'] = pruned
    value['ce_menu'] = json.dumps(items)
    setting.value = value
    setting.save()


class Migration(migrations.Migration):

    dependencies = [
        ('student_onboarding', '0002_studentonboarding_last_notified_on_and_more'),
        ('cis', '__first__'),
    ]

    operations = [
        migrations.RunPython(add_menu_item, remove_menu_item),
    ]
