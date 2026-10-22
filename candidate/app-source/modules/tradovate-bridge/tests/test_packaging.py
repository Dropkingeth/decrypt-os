"""runtime-files.txt lists exactly what the image installs: every shipped file, no tests or fixtures."""
import os
import re
import unittest
from pathlib import Path

PROFILE_EXCEPTION = re.compile(r'(?i)\b(?:prop[_-]?)?profile[_-]?(?:exception|exemption|override)s?\b'
                               r'|\b(?:per[_-])?account[_-]?(?:exception|exemption)s?\b')
MODULE = Path(__file__).resolve().parent.parent
NOT_SHIPPED = re.compile(r'(^|/)tests/|(^|/)test_[^/]*$|/fixtures/|^admin/tools/|^admin/app/ui/mock/|(^|/)\.|'
                         r'__pycache__|\.pyc$')
# Written without a literal '/home/<name>/' so the repo privacy gate does not need to exempt this file.
HOME_DIR = re.compile(r'/(?:home)/([^/\s"\']+)/')
PLACEHOLDER_HOMES = {'trader', 'customer', 'user'}


class RuntimeFiles(unittest.TestCase):
    def listed(self):
        return (MODULE / 'runtime-files.txt').read_text().split()

    def test_list_matches_the_module_tree(self):
        tree = sorted(str(p.relative_to(MODULE)) for p in MODULE.rglob('*')
                      if p.is_file() and not NOT_SHIPPED.search(str(p.relative_to(MODULE))))
        self.assertEqual(sorted(self.listed()), tree)

    def test_units_have_one_execstart_and_no_owner_paths(self):
        units = [n for n in self.listed() if n.startswith('systemd/')]
        self.assertEqual(len(units), 13)
        for name in units:
            text = (MODULE / name).read_text()
            starts = [ln for ln in text.splitlines() if ln.startswith('ExecStart')]
            self.assertEqual(len(starts) if name.endswith('.service') else len(starts) + 1, 1, name)
            self.assertNotIn('ExecStartPre', text, name)
            self.assertNotIn('/home/', text, name)
            for path in re.findall(r'/usr/local/lib/decrypt-os/modules/tradovate-bridge/(\S+)', text):
                self.assertTrue(path in self.listed() or any(f.startswith(path + '/') for f in self.listed()),
                                (name, path))

    def test_shipped_code_has_no_host_paths_or_profile_exceptions(self):
        # Prop profiles apply per profile, never per account: shipped code carries no account-level
        # exception, exemption or override to a profile rule.
        for name in self.listed():
            text = (MODULE / name).read_text()
            self.assertNotIn('/Users/', text, name)
            self.assertIsNone(PROFILE_EXCEPTION.search(text), name)
            # Example configs may only use documented placeholder homes; any other
            # /home/<name>/ would be a real build or dev host's account.
            for home in HOME_DIR.findall(text):
                self.assertIn(home, PLACEHOLDER_HOMES, name)


if __name__ == '__main__':
    unittest.main()
