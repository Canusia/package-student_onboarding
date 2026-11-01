"""The student_onboarding package's sidebar items are in the cis menu registry (menu.py, from ready()).

Ids are the legacy menu names; `<id>_blurb` portal-text fields,
`active_menu` values and converted layouts' `adopt` paths depend on them.
"""
import importlib.util
import unittest

from django.test import SimpleTestCase

HAS_REGISTRY = importlib.util.find_spec('cis.menu_registry') is not None
SOURCE = 'student_onboarding'

EXPECTED = {
    'ce': {'students/pending_onboarding_notifications': (
        'Onboarding Reminders', 'student_onboarding_ce:pending_notifications', 75)},
}


def _paths(role):
    from cis.menu_registry import menu
    out = {}
    for item in menu.items(role):
        if item['source'] == SOURCE:
            out[item['name']] = item
        for sub in item['sub_menu']:
            if sub.get('source') == SOURCE and sub['type'] == 'nav-item':
                out[f"{item['name']}/{sub['name']}"] = sub
    return out


@unittest.skipUnless(HAS_REGISTRY, 'cis has no menu registry')
class MenuItemsTests(SimpleTestCase):

    def test_items_registered(self):
        for role, expected in EXPECTED.items():
            got = {path: (i['label'], i['url'], i['order'])
                   for path, i in _paths(role).items()}
            self.assertEqual(got, expected, role)

    def test_no_registry_problems(self):
        from cis.menu_registry import check_menu_registry, menu
        problems = [m for m in check_menu_registry(menu)
                    if m.id.startswith('cis.E_MENU') and f'({SOURCE})' in m.msg]
        self.assertEqual(problems, [])
