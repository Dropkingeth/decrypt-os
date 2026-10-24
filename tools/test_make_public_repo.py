"""End-to-end tests for tools/make_public_repo.sh against a disposable
synthetic source repo -- never the real repo (a development repo's full
history can legitimately fail --history with contributors' own identities;
see tools/test_privacy_check.py's HistoryModeTests). The script resolves its
source repo from its own file location (same convention as build/build.sh),
so each test copies the real script + privacy_check.py into a throwaway git
repo and invokes it from there."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'make_public_repo.sh'

# A synthetic stand-in for the owner's real DECRYPT_PRIVATE_PATTERNS file, so
# that every test below runs with the env var the script now requires
# regardless of whether the ambient shell happens to export it.
_patterns_fd, _patterns_path = tempfile.mkstemp(suffix='-private-patterns.txt')
with os.fdopen(_patterns_fd, 'w') as _fh:
    _fh.write('synthetic-test-private-pattern-not-a-real-secret\n')
DEFAULT_PATTERNS = Path(_patterns_path)


def make_source_repo(root, extra_files=None):
    root = Path(root)
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'dev@example.invalid'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Dev'], cwd=root, check=True)
    (root / 'README.md').write_text('Decrypt OS test fixture.\n')
    tools_dir = root / 'tools'
    tools_dir.mkdir()
    shutil.copyfile(SCRIPT, tools_dir / 'make_public_repo.sh')
    (tools_dir / 'make_public_repo.sh').chmod(0o755)
    shutil.copyfile(HERE / 'privacy_check.py', tools_dir / 'privacy_check.py')
    for name, content in (extra_files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    # A non-UTC source commit, like a maintainer's local one.
    env = dict(os.environ, GIT_AUTHOR_DATE='1760000000 +0200', GIT_COMMITTER_DATE='1760000000 +0200')
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=root, check=True, env=env)
    subprocess.run(['git', 'tag', 'v1'], cwd=root, check=True)
    return root


def run_script(src, tag, out, env=None):
    run_env = dict(os.environ)
    run_env.setdefault('DECRYPT_PRIVATE_PATTERNS', str(DEFAULT_PATTERNS))
    if env is not None:
        run_env = env
    return subprocess.run(['bash', str(src / 'tools/make_public_repo.sh'), tag, str(out)],
                           capture_output=True, text=True, env=run_env)


class MakePublicRepoTests(unittest.TestCase):
    def test_creates_a_single_orphan_commit_authored_by_decrypt_labs(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            result = run_script(src, 'v1', out)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            log = subprocess.run(['git', '-C', out, 'log', '--all', '--format=%an <%ae> | %cn <%ce>'],
                                  capture_output=True, text=True, check=True).stdout.strip().splitlines()
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0], 'Decrypt Labs <noreply@decryptlabs.io> | Decrypt Labs <noreply@decryptlabs.io>')

            files = subprocess.run(['git', '-C', out, 'ls-files'],
                                    capture_output=True, text=True, check=True).stdout.split()
            self.assertIn('README.md', files)
            self.assertIn('tools/make_public_repo.sh', files)
            # The source repo's own git history (its real "Dev" identity) is
            # not reachable: the output repo was `git init`-ed fresh.
            refs = subprocess.run(['git', '-C', out, 'rev-list', '--all'],
                                   capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(len(refs), 1)

    def test_creates_an_annotated_tag_with_the_decrypt_labs_identity(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            result = run_script(src, 'v1', out)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            tags = subprocess.run(['git', '-C', out, 'tag'], capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(tags, ['v1'])
            tagger = subprocess.run(['git', '-C', out, 'for-each-ref', 'refs/tags/v1',
                                      '--format=%(taggername) %(taggeremail)'],
                                     capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(tagger, 'Decrypt Labs <noreply@decryptlabs.io>')

    def test_commit_and_tag_dates_are_utc_midnight_even_from_a_non_utc_host(self):
        # The source repo's own commit is made at +0200 and the script runs
        # with a non-UTC TZ; the public commit and tag must still be +0000,
        # at 00:00:00 UTC of the day the script runs (date only, no time of day).
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            env = dict(os.environ, TZ='America/Denver', DECRYPT_PRIVATE_PATTERNS=str(DEFAULT_PATTERNS))
            days = {int(time.time()) // 86400 * 86400}
            result = subprocess.run(['bash', str(src / 'tools/make_public_repo.sh'), 'v1', str(out)],
                                    capture_output=True, text=True, env=env)
            days.add(int(time.time()) // 86400 * 86400)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dates = subprocess.run(['git', '-C', out, 'log', '--all', '--format=%ad %cd', '--date=raw'],
                                    capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(len(dates), 4)
            self.assertEqual(dates[1::2], ['+0000', '+0000'])
            self.assertEqual(len(set(dates[0::2])), 1)
            self.assertIn(int(dates[0]), days)
            tagger = subprocess.run(['git', '-C', out, 'for-each-ref', 'refs/tags/v1',
                                      '--format=%(taggerdate:raw)'],
                                     capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(tagger[1], '+0000')
            self.assertEqual(tagger[0], dates[0])

    def test_refuses_to_run_without_decrypt_private_patterns(self):
        # Without DECRYPT_PRIVATE_PATTERNS, the owner's real private patterns
        # are never checked at all -- the script must fail loudly rather than
        # silently falling back to privacy_check.py's built-in patterns.
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            env = {k: v for k, v in os.environ.items() if k != 'DECRYPT_PRIVATE_PATTERNS'}
            result = run_script(src, 'v1', out, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DECRYPT_PRIVATE_PATTERNS', result.stderr)
            self.assertFalse(out.exists())

    def test_refuses_when_decrypt_private_patterns_is_not_a_file(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            env = dict(os.environ, DECRYPT_PRIVATE_PATTERNS=str(Path(out_parent) / 'does-not-exist.txt'))
            result = run_script(src, 'v1', out, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DECRYPT_PRIVATE_PATTERNS', result.stderr)
            self.assertFalse(out.exists())

    def test_refuses_an_unknown_tag(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'public'
            result = run_script(src, 'no-such-tag', out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('no such tag', result.stderr)
            self.assertFalse(out.exists())

    def test_refuses_an_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp)
            out = Path(out_parent) / 'already-here'
            out.mkdir()
            result = run_script(src, 'v1', out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('already exists', result.stderr)

    def test_refuses_an_output_directory_inside_the_repo(self):
        with tempfile.TemporaryDirectory() as src_tmp:
            src = make_source_repo(src_tmp)
            out = src / 'nested-output'
            result = run_script(src, 'v1', out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('outside this repo checkout', result.stderr)

    def test_privacy_check_history_failure_propagates(self):
        # A real secret shape committed at the tag must make privacy_check's
        # --history gate (run inside the script) fail, and the script with it.
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as out_parent:
            src = make_source_repo(src_tmp, extra_files={'leak.txt': 'KEY = "AKIAABCDEFGHIJKLMNOP"\n'})
            out = Path(out_parent) / 'public'
            result = run_script(src, 'v1', out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('AWS access key id', result.stderr)


if __name__ == '__main__':
    unittest.main()
