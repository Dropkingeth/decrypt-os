"""Real SQLite onboarding checks. Disposable identities only; no providers."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('optional_setup_server', ROOT/'app/backend/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

OFFLINE = {'requestUrl': '', 'publicSite': 'https://decryptlabs.io'}


class OptionalSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='decrypt-optional-')
        self.addCleanup(self.temp.cleanup)
        self.store = server.Store(self.temp.name, OFFLINE)

    def advance(self, trading=False):
        self.store.onboarding({'step':'assistant'})
        return self.store.onboarding({'step':'bot' if trading else 'connections', 'assistantName':'Fixture Atlas', 'tradingPreferences':trading})

    def review(self):
        self.advance()
        return self.store.onboarding({'step':'review'})

    def test_fresh_identity_has_no_trading_choice_or_completion(self):
        state = self.store.state()
        self.assertEqual(state['onboarding'], {'version':2,'step':'welcome','tradingPreferences':False,'preferencesComplete':False,'completed':[]})

    def test_assistant_only_finishes_without_request_or_provider(self):
        self.review()
        state = self.store.onboarding({'step':'review','complete':True})
        self.assertTrue(state['onboarding']['preferencesComplete'])
        self.assertEqual(state['onboarding']['completed'],list(server.CORE_STEPS))
        self.assertIsNone(state['strategyAccess']['latest'])
        self.assertTrue(all(v == 'not_connected' for v in state['connections'].values()))
        self.assertTrue(all(b['status'] == 'not_connected' for b in state['bots']))

    def test_forward_skip_is_rejected_and_state_unchanged(self):
        before = self.store.state()
        with self.assertRaises(server.ApiError):
            self.store.onboarding({'step':'review','assistantName':'Fixture Atlas'})
        self.assertEqual(before,self.store.state())

    def test_name_required_before_connections(self):
        self.store.onboarding({'step':'assistant'})
        with self.assertRaisesRegex(server.ApiError,'assistant name'):
            self.store.onboarding({'step':'connections'})

    def test_optional_checkpoint_requires_explicit_choice(self):
        self.store.onboarding({'step':'assistant'})
        with self.assertRaisesRegex(server.ApiError,'optional trading'):
            self.store.onboarding({'step':'bot','assistantName':'Fixture Atlas'})

    def test_trading_route_can_finish_without_access_request(self):
        self.advance(True)
        for step in ('connections','access','review'):
            self.store.onboarding({'step':step})
        state = self.store.onboarding({'step':'review','complete':True})
        self.assertEqual(state['onboarding']['completed'],list(server.STEPS))
        self.assertIsNone(state['strategyAccess']['latest'])

    def test_cannot_skip_new_trading_checkpoint(self):
        self.store.onboarding({'step':'assistant'})
        with self.assertRaisesRegex(server.ApiError,'preceding'):
            self.store.onboarding({'step':'connections','tradingPreferences':True,'assistantName':'Fixture Atlas'})
        self.assertFalse(self.store.state()['onboarding']['tradingPreferences'])

    def test_completion_requires_previously_saved_review(self):
        self.advance()
        with self.assertRaisesRegex(server.ApiError,'Review your saved'):
            self.store.onboarding({'step':'review','complete':True})
        self.assertEqual(self.store.state()['onboarding']['step'],'connections')

    def test_completion_idempotent_and_persists_restart(self):
        self.review()
        expected = self.store.onboarding({'step':'review','complete':True})
        self.assertEqual(expected,self.store.onboarding({'step':'review','complete':True}))
        self.assertEqual(expected,server.Store(self.temp.name, OFFLINE).state())

    def test_editing_name_invalidates_completion(self):
        self.review()
        self.store.onboarding({'step':'review','complete':True})
        result=self.store.onboarding({'step':'review','assistantName':'Fixture Other'})
        self.assertFalse(result['onboarding']['preferencesComplete'])

    def test_cannot_finish_and_change_unreviewed_preferences_together(self):
        before=self.review()
        for choice in ({'assistantName':'Fixture Other'},{'tradingPreferences':True}):
            with self.subTest(choice=choice),self.assertRaisesRegex(server.ApiError,'review them'):
                self.store.onboarding({'step':'review','complete':True,**choice})
        self.assertEqual(before,self.store.state())

    def test_returning_to_earlier_checkpoint_invalidates_completion(self):
        self.review()
        self.store.onboarding({'step':'review','complete':True})
        self.assertFalse(self.store.onboarding({'step':'assistant'})['onboarding']['preferencesComplete'])

    def test_choice_fields_reject_strings_numbers_and_null(self):
        for key in ('tradingPreferences','complete'):
            for value in ('true',1,None,{},[]):
                with self.subTest(key=key,value=value),self.assertRaises(server.ApiError):
                    self.store.onboarding({'step':'welcome',key:value})

    def test_credential_and_verification_fields_rejected(self):
        for key in ('apiKey','verified','tradingReady','providerVerified','password'):
            with self.subTest(key=key),self.assertRaises(server.ApiError):
                self.store.onboarding({'step':'welcome',key:'fixture'})

    def test_setup_username_is_saved_locally_and_never_sent(self):
        self.advance(True)
        state=self.store.onboarding({'step':'connections','tradingViewUsername':'fixture_user'})
        self.assertEqual(state['profile']['tradingViewUsername'],'fixture_user')
        self.assertEqual(state['strategyAccess']['tradingViewUsername'],'fixture_user')
        self.assertIsNone(state['strategyAccess']['latest'])
        # A different name can be saved later; there is no per-system binding.
        state=self.store.onboarding({'step':'connections','tradingViewUsername':'other_user'})
        self.assertEqual(state['profile']['tradingViewUsername'],'other_user')

    def test_opt_out_at_access_resumes_core_review(self):
        self.advance(True)
        for step in ('connections','access'):
            self.store.onboarding({'step':step})
        state=self.store.onboarding({'step':'review','tradingPreferences':False})
        self.assertEqual(state['onboarding']['completed'],['welcome','assistant','connections'])

    def test_saved_preferences_add_fixed_events_without_free_text(self):
        self.review()
        state = self.store.onboarding({'step': 'review', 'complete': True})
        self.assertTrue(state['activity'])
        messages = [row['message'] for row in state['activity']]
        self.assertIn('Setup preferences completed.', messages)
        self.assertTrue(all(message in ('Setup preferences completed.', 'Setup preferences saved.') for message in messages))
        self.assertNotIn('Fixture Atlas', str(state['activity']))

    def test_idempotent_preferences_do_not_duplicate_activity(self):
        self.review()
        state = self.store.onboarding({'step': 'review', 'complete': True})
        repeated = self.store.onboarding({'step': 'review', 'complete': True})
        self.assertEqual(repeated['activity'], state['activity'])
        self.assertEqual(server.Store(self.temp.name, OFFLINE).state()['activity'], state['activity'])

    def test_legacy_migration_never_infers_opt_in(self):
        self.advance(True)
        for previous in server.STEPS:
            with self.subTest(previous=previous):
                with self.store.connect() as db:
                    db.execute("DELETE FROM settings WHERE key IN ('onboardingVersion','tradingPreferences','preferencesComplete')")
                    db.execute("UPDATE settings SET value=? WHERE key='step'",(previous,))
                migrated=server.Store(self.temp.name, OFFLINE).state()
                self.assertEqual(migrated['onboarding']['step'],{'bot':'connections','access':'review'}.get(previous,previous))
                self.assertFalse(migrated['onboarding']['tradingPreferences'])
                self.assertFalse(migrated['onboarding']['preferencesComplete'])
                self.assertEqual(migrated['profile']['assistantName'],'Fixture Atlas')
                self.assertEqual(migrated,server.Store(self.temp.name, OFFLINE).state())

if __name__ == '__main__':
    unittest.main()
