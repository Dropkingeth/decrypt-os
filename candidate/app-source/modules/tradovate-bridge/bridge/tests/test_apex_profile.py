"""Apex accounts are never automated: plain "apex" is refused and there is no per-account exception."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tvbridge import firing, followers  # noqa: E402


class ApexProfile(unittest.TestCase):
    def follower(self, profile):
        return {'key': 'account-b', 'account_name': 'DEMO-0002', 'account_id': 100002,
                'journal_dir': '/tmp/j', 'kill_switch_file': '/tmp/k', 'max_contracts': 10,
                'prop_profile': profile}

    def test_followers_refuse_apex_and_any_unknown_profile(self):
        for name in ('apex', 'Apex', 'APEX', 'apex-exception', 'unrestricted-exception', ''):
            with self.subTest(name=name), self.assertRaises(followers.FollowerConfigError):
                followers.parse({'followers': [self.follower(name)]})

    def test_profiles_are_generic(self):
        self.assertEqual(followers.PROP_PROFILES, ('cross-account-window', 'unrestricted'))
        self.assertEqual(set(firing.PROP_PROFILES), {'cross-account-window', 'unrestricted'})
        self.assertIn('apex', firing.REFUSED_PROFILES)


if __name__ == '__main__':
    unittest.main()
