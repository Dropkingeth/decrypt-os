"""The four retired recovery entry points stay retired (see RETIRED-RECOVERY.md).

Extracted from an earlier, since-removed image-factory test module. This one
check is kept on its own because
it protects a documented, still-current invariant: these files are
intentionally preserved as inert stubs, never reconstructed or revived, and
must keep refusing before any operation.
"""
from pathlib import Path
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent


class RetiredEntrypointTests(unittest.TestCase):
    def test_retired_recovery_scripts_refuse_without_any_native_call(self):
        for name in ('bootstrap_iso9660.sh', 'install-abi13-compat.sh',
                     'restore_live_firewall_modules.py', 'resume_pacstrap.py'):
            with self.subTest(name=name):
                argv = ['bash' if name.endswith('.sh') else sys.executable, str(HERE / name), '--apply']
                result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('retired', result.stderr)

    def test_network_policy_guest_policy_flags_are_retired(self):
        for flag in ('--apply-guest-policy', '--resume-guest-policy'):
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, str(HERE / 'network_policy.py'), flag],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('retired', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
