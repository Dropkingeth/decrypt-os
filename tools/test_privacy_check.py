"""Synthetic fixtures only -- never scans the real repo (that would make this
test's result depend on unrelated future edits elsewhere in the tree).
Builds a throwaway git repo per test so tracked_files() (git ls-files, the
exact set build/build.sh would archive and ship) has something to list."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import privacy_check as pc


class FixtureRepo:
    def __init__(self, files, name='Test', email='test@example.invalid'):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.email', email], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.name', name], cwd=self.root, check=True)
        self.write(files)

    def write(self, files):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)

    def commit(self, message='commit', date='1760000000 +0000'):
        # Pinned UTC date by default: --history rejects any non-+0000 offset,
        # and the host's local timezone must not decide these tests' outcome.
        env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run(['git', 'commit', '-q', '-m', message], cwd=self.root, check=True, env=env)

    def tag(self, name, date='1760000000 +0000'):
        env = dict(os.environ, GIT_COMMITTER_DATE=date)
        subprocess.run(['git', 'tag', '-a', name, '-m', name], cwd=self.root, check=True, env=env)

    def __enter__(self):
        return self.root

    def __exit__(self, *exc):
        self.tmp.cleanup()


class PrivacyCheckTests(unittest.TestCase):
    def findings(self, files):
        with FixtureRepo(files) as root:
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                total = pc.run(root)
            return total, buf.getvalue()

    # --- things that must be caught ---

    def test_private_key_header_is_caught(self):
        total, out = self.findings({'secret.txt': '-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n'})
        self.assertEqual(total, 1)
        self.assertIn('private key material', out)

    def test_aws_access_key_is_caught(self):
        total, out = self.findings({'conf.py': "KEY = 'AKIAABCDEFGHIJKLMNOP'\n"})
        self.assertEqual(total, 1)
        self.assertIn('AWS access key id', out)

    def test_real_looking_email_is_caught(self):
        total, out = self.findings({'notes.md': 'Contact jane.doe@realcompany.example for access.\n'})
        self.assertEqual(total, 1)
        self.assertIn('email', out)

    def test_macos_user_path_is_caught(self):
        total, out = self.findings({'script.sh': 'cd /Users/someperson/project\n'})
        self.assertEqual(total, 1)
        self.assertIn('macOS host path', out)

    def test_macos_user_path_with_no_trailing_slash_is_caught(self):
        # T3 regression: a bare path at end-of-line (no trailing '/') must
        # still match, not just '/Users/<name>/'.
        total, out = self.findings({'notes.md': 'Logs were under /Users/someperson\n'})
        self.assertEqual(total, 1)
        self.assertIn('macOS host path', out)

    def test_real_home_username_is_caught(self):
        total, out = self.findings({'script.sh': 'cp -r /home/realowner/data /tmp\n'})
        self.assertEqual(total, 1)
        self.assertIn('/home/<name>', out)

    def test_home_username_with_no_trailing_slash_is_caught(self):
        # T3 regression: same no-trailing-slash fix as the macOS path above.
        total, out = self.findings({'notes.md': 'Logs were under /home/realowner\n'})
        self.assertEqual(total, 1)
        self.assertIn('/home/<name>', out)

    def test_nonfake_secret_literal_is_caught(self):
        total, out = self.findings(
            {'conf.py': "password = 'xK9mQ2vL8pR4tN6wZ1cY3bH7'\n"})
        self.assertEqual(total, 1)
        self.assertIn('secret/token/key/password literal', out)

    def test_secret_value_with_marker_word_embedded_mid_run_is_caught(self):
        # T4 regression: a real-looking 20+ char secret whose value merely
        # contains "fake" as a substring with no delimiter on either side
        # (e.g. inside a base64-ish blob) must not be waved through as a
        # placeholder just because the word appears somewhere in it.
        # Confirmed against the pre-fix regex directly that this exact
        # value WAS wrongly exempted before narrowing FAKE_VALUE_MARKERS to
        # \b-anchored whole words.
        total, out = self.findings(
            {'conf.py': "password = 'xZ9qPfakeM3vL8rN4wQ1cY7bH2kD6'\n"})
        self.assertEqual(total, 1)
        self.assertIn('secret/token/key/password literal', out)

    def test_two_matches_on_one_line_are_both_reported(self):
        total, out = self.findings({'t.py': "KEY = 'AKIAABCDEFGHIJKLMNOP AKIAZZZZZZZZZZZZZZZZ'\n"})
        self.assertEqual(total, 2)
        self.assertEqual(out.count('AWS access key id'), 2)

    def test_unlisted_extension_is_still_scanned(self):
        # .lua is not in any suffix allowlist -- there is no suffix allowlist.
        total, out = self.findings({'config.lua': "token = 'AKIAABCDEFGHIJKLMNOP'\n"})
        self.assertEqual(total, 1)

    def test_extensionless_file_is_still_scanned(self):
        total, out = self.findings({'Dockerfile': "ENV KEY=AKIAABCDEFGHIJKLMNOP\n"})
        self.assertEqual(total, 1)

    def test_admin_email_on_unsafe_domain_is_caught(self):
        total, out = self.findings({'notes.md': 'Contact admin@realcompany.io.\n'})
        self.assertEqual(total, 1)
        self.assertIn('email', out)

    def test_root_email_on_unsafe_domain_is_caught(self):
        total, out = self.findings({'notes.md': 'root@prod-server.io is the contact.\n'})
        self.assertEqual(total, 1)

    def test_github_fine_grained_token_is_caught(self):
        total, out = self.findings({'t.py': "VALUE = 'github_pat_" + 'A' * 32 + "'\n"})
        self.assertEqual(total, 1)
        self.assertIn('GitHub fine-grained token', out)

    def test_github_oauth_style_token_is_caught(self):
        total, out = self.findings({'t.py': "VALUE = 'gho_" + 'A' * 36 + "'\n"})
        self.assertEqual(total, 1)
        self.assertIn('GitHub token', out)

    def test_jwt_is_caught(self):
        jwt = 'eyJ' + 'A' * 20 + '.' + 'B' * 20 + '.' + 'C' * 20
        total, out = self.findings({'t.py': f"TOKEN = '{jwt}'\n"})
        self.assertEqual(total, 1)
        self.assertIn('JWT', out)

    def test_bearer_token_is_caught(self):
        total, out = self.findings({'t.py': "HEADER = 'Bearer " + 'A' * 25 + "'\n"})
        self.assertEqual(total, 1)
        self.assertIn('Bearer token', out)

    def test_telegram_bot_token_is_caught(self):
        total, out = self.findings({'t.py': "TOKEN = '" + '1' * 9 + ':' + 'A' * 35 + "'\n"})
        self.assertEqual(total, 1)
        self.assertIn('Telegram', out)

    def test_discord_token_is_caught(self):
        token = 'M' + 'A' * 24 + '.' + 'B' * 6 + '.' + 'C' * 27
        total, out = self.findings({'t.py': f"TOKEN = '{token}'\n"})
        self.assertEqual(total, 1)
        self.assertIn('Discord', out)

    def test_tailscale_cgnat_address_is_caught(self):
        total, out = self.findings({'t.py': "HOST = '100.64.1.5'\n"})
        self.assertEqual(total, 1)
        self.assertIn('Tailscale', out)

    def test_tailscale_hostname_is_caught(self):
        total, out = self.findings({'t.py': "HOST = 'myhost.tailnet.ts.net'\n"})
        self.assertEqual(total, 1)
        self.assertIn('ts.net', out)

    def test_dot_local_hostname_is_caught(self):
        total, out = self.findings({'t.py': "HOST = 'someones-mac-mini.local'\n"})
        self.assertEqual(total, 1)
        self.assertIn('.local', out)

    def test_encrypted_private_key_header_is_caught(self):
        total, out = self.findings({'k.pem': '-----BEGIN ENCRYPTED PRIVATE KEY-----\n'})
        self.assertEqual(total, 1)

    def test_dsa_private_key_header_is_caught(self):
        total, out = self.findings({'k.pem': '-----BEGIN DSA PRIVATE KEY-----\n'})
        self.assertEqual(total, 1)

    def test_pgp_private_key_block_is_caught(self):
        total, out = self.findings({'k.pem': '-----BEGIN PGP PRIVATE KEY BLOCK-----\n'})
        self.assertEqual(total, 1)

    def test_pine_file_extension_is_always_caught(self):
        total, out = self.findings({'strategy.pine': 'anything at all\n'})
        self.assertEqual(total, 1)
        self.assertIn('.pine extension', out)

    def test_pine_file_extension_is_caught_even_when_empty(self):
        total, out = self.findings({'empty.pine': ''})
        self.assertEqual(total, 1)
        self.assertIn('.pine extension', out)

    def test_pine_version_header_is_caught_in_a_non_pine_extension_file(self):
        total, out = self.findings({'notes.txt': '//@version=5\nstrategy("x")\n'})
        self.assertEqual(total, 1)
        self.assertIn('//@version=', out)

    def test_pine_version_header_after_leading_comment_lines_is_still_caught(self):
        # Real Pine files commonly start with a license/author comment block
        # before the version pragma, not on line 1.
        total, out = self.findings(
            {'notes.txt': '// Copyright (c) 2026\n// mit license\n//@version=5\nstrategy("x")\n'})
        self.assertEqual(total, 1)
        self.assertIn('//@version=', out)

    def test_undecodable_pine_extension_file_is_still_caught(self):
        with FixtureRepo({}) as root:
            (root / 'strategy.pine').write_bytes(b'\xff\xfe\x00binary pine bytes')
            subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                total = pc.run(root)
            out = buf.getvalue()
        self.assertEqual(total, 1)
        self.assertIn('.pine extension', out)

    def test_pine_declaration_with_namespace_usage_is_caught(self):
        total, out = self.findings(
            {'notes.txt': 'strategy("ICT Turtle Soup", overlay=true)\nlong = ta.crossover(a, b)\n'})
        self.assertEqual(total, 1)
        self.assertIn('Pine namespace usage', out)

    def test_pine_declaration_with_input_namespace_is_caught(self):
        total, out = self.findings(
            {'notes.txt': 'indicator("FVG", overlay=true)\nlen = input.int(14, "Length")\n'})
        self.assertEqual(total, 1)
        self.assertIn('Pine namespace usage', out)

    def test_extra_pattern_from_env_file_is_caught(self):
        with tempfile.TemporaryDirectory() as extra_dir:
            extra_file = Path(extra_dir) / 'private-patterns.txt'
            extra_file.write_text('# comment\nOwnerRealName\n')
            os.environ['DECRYPT_PRIVATE_PATTERNS'] = str(extra_file)
            try:
                total, out = self.findings({'notes.md': 'Built by OwnerRealName on release day.\n'})
            finally:
                del os.environ['DECRYPT_PRIVATE_PATTERNS']
            self.assertEqual(total, 1)
            self.assertIn('extra pattern', out)

    def test_public_repo_url_is_masked_but_handle_elsewhere_is_caught(self):
        handle = pc.PUBLIC_REPO_URL.split('/')[3]
        with tempfile.TemporaryDirectory() as extra_dir:
            extra_file = Path(extra_dir) / 'private-patterns.txt'
            extra_file.write_text(handle + '\n')
            os.environ['DECRYPT_PRIVATE_PATTERNS'] = str(extra_file)
            try:
                clean, _ = self.findings({'a.yml': f'url: {pc.PUBLIC_REPO_URL}/issues/new\n'})
                total, _ = self.findings({'b.md': f'Maintained by {handle}.\n'})
            finally:
                del os.environ['DECRYPT_PRIVATE_PATTERNS']
            self.assertEqual(clean, 0)
            self.assertEqual(total, 1)

    def test_tradingview_chart_layout_link_is_caught(self):
        total, out = self.findings({'notes.md': 'Layout: https://www.tradingview.com/chart/Zx9Qw7Lm/\n'})
        self.assertEqual(total, 1)
        self.assertIn('TradingView chart layout link', out)

    def test_extra_pattern_still_runs_on_an_exempt_file(self):
        old_exempt = dict(pc.EXEMPT_FILES)
        pc.EXEMPT_FILES['notices.txt'] = 'synthetic test exemption'
        with tempfile.TemporaryDirectory() as extra_dir:
            extra_file = Path(extra_dir) / 'private-patterns.txt'
            extra_file.write_text('OwnerRealName\n')
            os.environ['DECRYPT_PRIVATE_PATTERNS'] = str(extra_file)
            try:
                total, out = self.findings({'notices.txt': (
                    'Copyright Real Person <real.person@realcompany.example>\n'
                    'Thanks OwnerRealName\n/home/someperson/x\n')})
            finally:
                del os.environ['DECRYPT_PRIVATE_PATTERNS']
                pc.EXEMPT_FILES.clear()
                pc.EXEMPT_FILES.update(old_exempt)
        # Only the owner pattern fires; the built-in email/home rules stay exempt.
        self.assertEqual(total, 1, out)
        self.assertIn('notices.txt:2: extra pattern', out)

    # --- things that must NOT be flagged (false-positive guards) ---

    def test_fixture_chart_ids_and_bare_chart_url_are_clean(self):
        total, out = self.findings({'t.py': (
            "url = 'https://www.tradingview.com/chart/Test1234/'\n"
            "other = 'https://www.tradingview.com/chart/AbCd5678/'\n"
            "launch = 'https://www.tradingview.com/chart/'\n")})
        self.assertEqual(total, 0, out)

    def test_example_domain_email_is_clean(self):
        total, _ = self.findings({'t.py': "USERNAME = 'fixture-user@example.invalid'\n"})
        self.assertEqual(total, 0)

    def test_public_role_addresses_are_clean(self):
        for address in pc.PUBLIC_ROLE_ADDRESSES:
            total, _ = self.findings({'t.md': f'Contact {address} for details.\n'})
            self.assertEqual(total, 0, address)

    def test_systemd_template_unit_is_not_an_email(self):
        total, _ = self.findings({'unit.service': 'After=tvbridge-browser@b.service\n'})
        self.assertEqual(total, 0)

    def test_generic_home_placeholder_is_clean(self):
        total, _ = self.findings({'doc.md': 'Config lives under /home/customer/.config\n'})
        self.assertEqual(total, 0)

    def test_short_home_test_variable_is_clean(self):
        total, _ = self.findings({'t.py': "root = '/home/x/state'\n"})
        self.assertEqual(total, 0)

    def test_fake_token_fixture_is_clean(self):
        total, _ = self.findings({'t.py': "TOKEN = 'account-view-token-0123456789abcdef'\n"})
        self.assertEqual(total, 0)

    def test_url_userinfo_placeholder_credentials_are_clean(self):
        total, _ = self.findings({'t.py': "url = 'https://user:pass@example.invalid/x'\n"})
        self.assertEqual(total, 0)

    def test_admin_email_on_a_safe_domain_is_clean(self):
        total, _ = self.findings({'t.py': "ADMIN = 'admin@example.invalid'\n"})
        self.assertEqual(total, 0)

    def test_root_email_on_a_safe_domain_is_clean(self):
        total, _ = self.findings({'t.py': "ROOT = 'root@customer.example.com'\n"})
        self.assertEqual(total, 0)

    def test_tailscale_cidr_range_definition_is_not_a_leak(self):
        total, _ = self.findings({'policy.py': "BLOCKED_V4 = ('100.64.0.0/10', '10.0.0.0/8')\n"})
        self.assertEqual(total, 0)

    def test_declaration_without_pine_namespace_usage_is_clean(self):
        # A function named strategy()/indicator() in another language, with
        # no accompanying Pine namespace call anywhere in the file, is not
        # enough signal on its own.
        total, _ = self.findings({'t.py': 'def run():\n    strategy("demo")\n'})
        self.assertEqual(total, 0)

    def test_common_identifiers_without_pine_declaration_are_clean(self):
        # math./str./array. etc. are ordinary identifiers in many languages;
        # without a Pine declaration alongside them, they are not Pine.
        total, _ = self.findings({'t.py': 'import math\nx = math.floor(str(array[0]))\n'})
        self.assertEqual(total, 0)

    def test_exempt_file_is_skipped_even_with_a_real_finding(self):
        content = 'Copyright 2024 Real Person <real.person@realcompany.example>\n'
        old_exempt = dict(pc.EXEMPT_FILES)
        pc.EXEMPT_FILES['notices.txt'] = 'synthetic test exemption'
        try:
            total, _ = self.findings({'notices.txt': content})
        finally:
            pc.EXEMPT_FILES.clear()
            pc.EXEMPT_FILES.update(old_exempt)
        self.assertEqual(total, 0)

    def test_same_content_is_caught_when_not_exempt(self):
        content = 'Copyright 2024 Real Person <real.person@realcompany.example>\n'
        total, _ = self.findings({'notices.txt': content})
        self.assertEqual(total, 1)

    def test_binary_file_is_skipped_without_crashing(self):
        with FixtureRepo({}) as root:
            (root / 'image.png').write_bytes(b'\x89PNG\r\n\x1a\n' + bytes(range(256)))
            subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
            total = pc.run(root)
        self.assertEqual(total, 0)

    def test_clean_tree_run_returns_zero_findings(self):
        with FixtureRepo({'readme.md': 'Nothing private here.\n'}) as root:
            self.assertEqual(pc.run(root), 0)

    # --- plain-text names of hashed contract inputs ---

    @staticmethod
    def contract(*names):
        import hashlib
        import json
        digests = {hashlib.sha256(n.encode()).hexdigest(): 'bool' for n in names}
        return json.dumps({'inputs': {'read_input': {'type': 'bool'}}, 'input_digests': digests}, indent=2) + '\n'

    def test_hashed_input_name_in_plain_text_is_caught(self):
        total, err = self.findings({'vendor/settings_contract.json': self.contract('fixture_secret_toggle'),
                                    'tests/alert.json': '{"inputs": {"fixture_secret_toggle": true}}\n',
                                    'tests/test_alert.py': "inputs['fixture_secret_toggle'] = False\n"})
        self.assertEqual(total, 2)
        self.assertIn('tests/alert.json:1: plain-text name of a hashed input in vendor/settings_contract.json', err)
        self.assertIn('tests/test_alert.py:1:', err)
        self.assertNotIn('fixture_secret_toggle', err)   # the finding never echoes the name

    def test_hashed_name_in_a_quoted_string_with_spaces_is_caught(self):
        total, _ = self.findings({'c.json': self.contract('Fixture Secret Label'),
                                  'notes.md': 'the "Fixture Secret Label" input\n'})
        self.assertEqual(total, 1)

    def test_hashed_name_is_caught_even_in_an_exempt_file(self):
        total, _ = self.findings({'c.json': self.contract('fixture_secret_toggle'),
                                  'tools/test_privacy_check.py': 'fixture_secret_toggle\n'})
        self.assertEqual(total, 1)

    def test_nested_and_list_digests_are_collected(self):
        import hashlib
        import json
        digest = hashlib.sha256(b'fixture_nested_name').hexdigest()
        files = {'c.json': json.dumps({'engine': {'input_digests': [digest.upper()]}}),
                 'a.txt': 'fixture_nested_name\n'}
        self.assertEqual(self.findings(files)[0], 1)

    def test_contract_alone_and_unrelated_names_are_clean(self):
        total, _ = self.findings({'vendor/settings_contract.json': self.contract('fixture_secret_toggle'),
                                  'tests/alert.json': '{"inputs": {"fixture_input_01": true, "read_input": true}}\n'})
        self.assertEqual(total, 0)

    def test_digest_rule_ignores_input_digests_outside_json(self):
        import hashlib
        digest = hashlib.sha256(b'fixture_secret_toggle').hexdigest()
        total, _ = self.findings({'notes.py': "input_digests = {'%s': 'bool'}\n" % digest,
                                  'a.txt': 'fixture_secret_toggle\n'})
        self.assertEqual(total, 0)


class HistoryModeTests(unittest.TestCase):
    """--history: full `git log --all -p --text` content scan plus every
    commit author/committer and annotated-tag tagger identity check. Public
    repo only -- see the module docstring."""

    def history_findings(self, root):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            total = pc.run_history(root)
        return total, buf.getvalue()

    def test_approved_identity_and_clean_history_passes(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 0, out)

    def test_unapproved_author_identity_is_caught(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Someone', email='someone@example.invalid')
        try:
            repo.commit('init')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertGreater(total, 0)
        self.assertIn('unapproved commit/tag identity', out)
        self.assertIn('Someone <someone@example.invalid>', out)

    def test_utc_annotated_tag_passes(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            repo.tag('v1')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 0, out)

    def test_non_utc_commit_offset_is_caught(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init', date='1760000000 +0200')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 2, out)  # author and committer
        self.assertIn('non-UTC commit/tag date', out)
        self.assertIn('author date', out)
        self.assertIn('committer date', out)
        self.assertIn('+0200', out)

    def test_non_utc_committer_offset_alone_is_caught(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            env = dict(os.environ, GIT_AUTHOR_DATE='1760000000 +0000', GIT_COMMITTER_DATE='1760000000 +0530')
            subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo.root, check=True, env=env)
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 1, out)
        self.assertIn('committer date', out)
        self.assertIn('+0530', out)

    def test_non_utc_tagger_offset_is_caught(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            repo.tag('v1', date='1760000000 +0200')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 1, out)
        self.assertIn('tag v1 tagger date', out)

    def test_offset_is_not_checked_outside_history_mode(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init', date='1760000000 +0200')
            self.assertEqual(pc.run(repo.root), 0)
        finally:
            repo.tmp.cleanup()

    def test_secret_removed_in_a_later_commit_is_still_caught_in_history(self):
        repo = FixtureRepo({'secret.txt': 'AKIAABCDEFGHIJKLMNOP\n'},
                            name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('add secret by mistake')
            repo.write({'secret.txt': 'removed\n'})
            repo.commit('remove secret')
            clean_tree_total = pc.run(repo.root)
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertEqual(clean_tree_total, 0)
        self.assertGreater(total, 0)
        self.assertIn('AWS access key id', out)

    def test_exempt_file_path_is_skipped_in_history_too(self):
        # tools/make_public_repo.sh's single orphan commit still contains an
        # already-exempted file (e.g. installer/test_customer_ingress.py's
        # 'a.local' fixture) in its diff; --history must not re-flag it.
        old_exempt = dict(pc.EXEMPT_FILES)
        pc.EXEMPT_FILES['secret.txt'] = 'synthetic test exemption'
        repo = FixtureRepo({'secret.txt': 'AKIAABCDEFGHIJKLMNOP\n'},
                            name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
            pc.EXEMPT_FILES.clear()
            pc.EXEMPT_FILES.update(old_exempt)
        self.assertEqual(total, 0, out)

    def _history_with_extra_pattern(self, repo, pattern):
        import os
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as fh:
            fh.write(pattern + '\n')
        old = os.environ.get('DECRYPT_PRIVATE_PATTERNS')
        os.environ['DECRYPT_PRIVATE_PATTERNS'] = fh.name
        try:
            return self.history_findings(repo.root)
        finally:
            if old is None:
                os.environ.pop('DECRYPT_PRIVATE_PATTERNS', None)
            else:
                os.environ['DECRYPT_PRIVATE_PATTERNS'] = old
            os.unlink(fh.name)

    def test_binary_file_bytes_are_skipped_in_history_like_the_tree_scan(self):
        # A short private pattern ('ZQ') appearing by chance inside binary
        # bytes forced through `git log --text` is not a text leak.
        repo = FixtureRepo({}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            (repo.root / 'image.png').write_bytes(
                b'\x89PNG\r\n\x1a\n\xff\xfe\nZQ\x00\x80\x81\n' + bytes(range(256)))
            subprocess.run(['git', 'add', '-A'], cwd=repo.root, check=True)
            repo.commit('init')
            tree_total = pc.run(repo.root)
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
        self.assertEqual(tree_total, 0)
        self.assertEqual(total, 0, out)

    def test_extra_pattern_in_text_is_still_caught_in_history(self):
        repo = FixtureRepo({'notes.txt': 'owner ZQ was here\n'},
                            name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 1, out)
        self.assertIn('history/notes.txt', out)

    def test_extra_pattern_in_exempt_file_is_caught_in_history(self):
        old_exempt = dict(pc.EXEMPT_FILES)
        pc.EXEMPT_FILES['notes.txt'] = 'synthetic test exemption'
        repo = FixtureRepo({'notes.txt': 'AKIAABCDEFGHIJKLMNOP owner ZQ\n'},
                            name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
            pc.EXEMPT_FILES.clear()
            pc.EXEMPT_FILES.update(old_exempt)
        self.assertEqual(total, 1, out)
        self.assertIn('history/notes.txt', out)
        self.assertNotIn('AWS access key id', out)

    def test_extra_pattern_in_annotated_tag_message_is_caught_in_history(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            env = dict(os.environ, GIT_COMMITTER_DATE='1760000000 +0000')
            subprocess.run(['git', 'tag', '-a', 'v1', '-m', 'release cut by ZQ'],
                           cwd=repo.root, check=True, env=env)
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
        self.assertEqual(total, 1, out)
        self.assertIn('history/tag-message v1', out)

    def test_extra_pattern_in_tag_name_is_caught_in_history(self):
        repo = FixtureRepo({'a.txt': 'hello\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('init')
            repo.tag('v1-ZQ')
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
        # The name is reported once as a tag name and once inside the
        # annotated tag's message (FixtureRepo.tag uses the name as message).
        self.assertIn('history/tag-name v1-ZQ', out)
        self.assertEqual(total, 2, out)

    def test_extra_pattern_in_a_removed_file_path_is_caught_in_history(self):
        repo = FixtureRepo({'docs/ZQ-notes.txt': 'hello\n'}, name='Decrypt Labs',
                           email='noreply@decryptlabs.io')
        try:
            repo.commit('add')
            subprocess.run(['git', 'rm', '-q', 'docs/ZQ-notes.txt'], cwd=repo.root, check=True)
            repo.commit('remove')
            total, out = self._history_with_extra_pattern(repo, 'ZQ')
        finally:
            repo.tmp.cleanup()
        self.assertIn('history/path', out)
        self.assertGreaterEqual(total, 1, out)

    def test_removed_pine_file_is_caught_in_history(self):
        repo = FixtureRepo({'s.pine': 'x = 1\n'}, name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('add')
            (repo.root / 's.pine').unlink()
            subprocess.run(['git', 'add', '-A'], cwd=repo.root, check=True)
            repo.commit('remove')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertGreater(total, 0)
        self.assertIn('Pine source file (.pine extension)', out)

    def test_removed_pine_source_in_other_extension_is_caught_in_history(self):
        repo = FixtureRepo({'s.txt': '//@version=5\nplot(close)\n'},
                            name='Decrypt Labs', email='noreply@decryptlabs.io')
        try:
            repo.commit('add')
            repo.write({'s.txt': 'gone\n'})
            repo.commit('remove')
            total, out = self.history_findings(repo.root)
        finally:
            repo.tmp.cleanup()
        self.assertGreater(total, 0)
        self.assertIn('//@version= header', out)


if __name__ == '__main__':
    unittest.main()
