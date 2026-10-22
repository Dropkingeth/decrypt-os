from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend'))
from guardian_auth import GuardianVault, PrivateFiles, GuardianError
from trading_ingress_secrets import IngressSecrets


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.files = PrivateFiles(self.temp.name); self.addCleanup(self.files.close)
        self.vault = GuardianVault(self.files)
        self.vault.initialize('FAKE-VAULT-PASSPHRASE-FOR-INGRESS')
        self.addCleanup(self.vault.lock)
        self.secrets = IngressSecrets(self.vault)

    def test_generated_secret_is_one_time_and_not_status_or_ciphertext(self):
        token = self.secrets.create('engine_fixture', 'pine')
        self.assertEqual(len(token), 43)
        self.assertNotIn(token, json.dumps(self.secrets.metadata()))
        self.assertNotIn(token, json.dumps(self.files.read(self.secrets.filename)))
        with self.assertRaises(GuardianError): self.secrets.create('engine_fixture', 'pine')
        with self.secrets.lease('engine_fixture', 'pine') as leased:
            self.assertEqual(token, leased)

    def test_producers_use_distinct_keys_and_signed_bytes(self):
        values = [self.secrets.create('engine_fixture', channel) for channel in ('pine', 'quote', 'bar')]
        self.assertEqual(len(set(values)), 3)
        for channel in ('pine', 'quote', 'bar'):
            with self.secrets.lease_signed('engine_fixture', channel) as value:
                self.assertIs(type(value), bytes)
                self.assertEqual(len(value), 32)

    def test_wrong_instance_and_channel_cannot_lease(self):
        self.secrets.create('engine_fixture', 'pine')
        for instance, channel in [('other', 'pine'), ('engine_fixture', 'quote'), ('../bad', 'pine'), ('engine_fixture', 'broker')]:
            with self.assertRaises(GuardianError):
                with self.secrets.lease(instance, channel): pass

    def test_vault_lock_blocks_recovery_until_explicit_unlock(self):
        token = self.secrets.create('engine_fixture', 'pine'); self.vault.lock()
        self.assertEqual(self.secrets.metadata()['producers'], None)
        with self.assertRaises(GuardianError):
            with self.secrets.lease('engine_fixture', 'pine'): pass
        self.vault.unlock('FAKE-VAULT-PASSPHRASE-FOR-INGRESS')
        with self.secrets.lease('engine_fixture', 'pine') as restored:
            self.assertEqual(restored, token)

    def test_route_ciphertext_cannot_be_imported_as_ingress(self):
        self.secrets.create('engine_fixture', 'pine')
        from trading_routes import RouteSecrets
        routes = RouteSecrets(self.vault)
        self.files.write(routes.filename, self.files.read(self.secrets.filename))
        with self.assertRaises(GuardianError): routes._load()


if __name__ == '__main__': unittest.main()
