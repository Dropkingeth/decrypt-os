"""Root-side customer vault migration never follows sandbox-planted links.

Runs unprivileged: the "sandbox" owner and the trusted parent owner are the
test's own uid, so the same fd-only code paths run without root.
"""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

HARNESS = Path(__file__).resolve().parents[1] / 'app/harness'
sys.path.insert(0, str(HARNESS))
spec = importlib.util.spec_from_file_location('vault_control_under_test', HARNESS / 'vault_control.py')
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

UID, GID = os.getuid(), os.getgid()
ACCOUNT = types.SimpleNamespace(pw_uid=UID, pw_gid=GID)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        os.chmod(self.base, 0o700)
        patcher = mock.patch.object(vc, 'TRUSTED_UID', UID)
        patcher.start(); self.addCleanup(patcher.stop)
        # Stand-ins: ROOT (sandbox runtime), a victim file root must never touch,
        # and the vault data directory.
        self.root = self.base / 'decrypt-os-customer-pilot'
        (self.root / 'runtime').mkdir(parents=True, mode=0o700)
        self.victim = self.base / 'victim'
        self.victim.write_text('do not touch')
        os.chmod(self.victim, 0o644)
        self.vault = self.base / 'vault'
        self.vault.mkdir(mode=0o700)
        self.data = self.vault / 'data'
        self.data.mkdir(mode=0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def victim_untouched(self):
        info = self.victim.lstat()
        self.assertEqual(self.victim.read_text(), 'do not touch')
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o644)

    def runtime_fds(self):
        root_fd, runtime_fd = vc.open_runtime_dirs(UID, root=self.root)
        self.addCleanup(os.close, root_fd); self.addCleanup(os.close, runtime_fd)
        return root_fd, runtime_fd


class RefreshMarker(Base):
    def test_planted_symlink_is_refused(self):
        (self.root / 'runtime/gateway-refresh-requested').symlink_to(self.victim)
        _, runtime_fd = self.runtime_fds()
        self.assertFalse(vc.request_gateway_refresh(runtime_fd, UID, GID))
        self.victim_untouched()
        self.assertTrue((self.root / 'runtime/gateway-refresh-requested').is_symlink())

    def test_dangling_symlink_creates_nothing(self):
        target = self.base / 'created-through-link'
        (self.root / 'runtime/gateway-refresh-requested').symlink_to(target)
        _, runtime_fd = self.runtime_fds()
        self.assertFalse(vc.request_gateway_refresh(runtime_fd, UID, GID))
        self.assertFalse(target.exists())

    def test_marker_is_created_private(self):
        _, runtime_fd = self.runtime_fds()
        self.assertTrue(vc.request_gateway_refresh(runtime_fd, UID, GID))
        info = (self.root / 'runtime/gateway-refresh-requested').lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)


class AuthLock(Base):
    def test_symlinked_lock_is_refused(self):
        (self.root / 'runtime/auth-operation.lock').symlink_to(self.victim)
        _, runtime_fd = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.open_runtime_file(runtime_fd, 'auth-operation.lock', UID, GID)
        self.victim_untouched()

    def test_hard_linked_lock_is_refused(self):
        os.link(self.victim, self.root / 'runtime/auth-operation.lock')
        _, runtime_fd = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.open_runtime_file(runtime_fd, 'auth-operation.lock', UID, GID)
        self.victim_untouched()

    def test_fifo_lock_is_refused_without_blocking(self):
        os.mkfifo(self.root / 'runtime/auth-operation.lock')
        _, runtime_fd = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.open_runtime_file(runtime_fd, 'auth-operation.lock', UID, GID)

    def test_regular_lock_opens(self):
        _, runtime_fd = self.runtime_fds()
        fd = vc.open_runtime_file(runtime_fd, 'auth-operation.lock', UID, GID)
        os.close(fd)
        self.assertTrue((self.root / 'runtime/auth-operation.lock').is_file())


class RuntimeDirectories(Base):
    def test_symlinked_runtime_directory_is_refused(self):
        (self.root / 'runtime').rmdir()
        (self.root / 'runtime').symlink_to(self.base)
        with self.assertRaises(RuntimeError):
            vc.open_runtime_dirs(UID, root=self.root)

    def test_symlinked_root_is_refused(self):
        real = self.base / 'elsewhere'
        self.root.rename(real)
        self.root.symlink_to(real)
        with self.assertRaises(RuntimeError):
            vc.open_runtime_dirs(UID, root=self.root)

    def test_foreign_owner_is_refused(self):
        with self.assertRaises(RuntimeError):
            vc.open_runtime_dirs(UID + 1, root=self.root)

    def test_group_writable_runtime_is_refused(self):
        os.chmod(self.root / 'runtime', 0o770)
        with self.assertRaises(RuntimeError):
            vc.open_runtime_dirs(UID, root=self.root)

    def test_untrusted_parent_is_refused(self):
        with mock.patch.object(vc, 'TRUSTED_UID', UID + 1):
            with self.assertRaises(RuntimeError):
                vc.open_runtime_dirs(UID, root=self.root)


class Copy(Base):
    def populate(self):
        (self.root / 'home/sub').mkdir(parents=True)
        (self.root / 'home/sub/a.txt').write_text('alpha')
        (self.root / 'state').mkdir()
        (self.root / 'state/b.json').write_text('{}')

    def test_copy_is_complete_and_private(self):
        self.populate()
        root_fd, _ = self.runtime_fds()
        result = vc.copy_durable(root_fd, self.data, ACCOUNT, UID)
        self.assertEqual(result['filesVerified'], 2)
        copied = self.data / 'home/sub/a.txt'
        self.assertEqual(copied.read_text(), 'alpha')
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.data / 'home/sub').stat().st_mode), 0o700)
        for name in vc.DURABLE:
            self.assertTrue((self.data / name).is_dir())

    def test_symlinked_file_is_refused(self):
        self.populate()
        (self.root / 'home/leak').symlink_to(self.victim)
        root_fd, _ = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.copy_durable(root_fd, self.data, ACCOUNT, UID)
        self.assertFalse((self.data / 'home/leak').exists())

    def test_symlinked_durable_directory_is_refused(self):
        (self.root / 'secrets').symlink_to(self.base)
        root_fd, _ = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.copy_durable(root_fd, self.data, ACCOUNT, UID)

    def test_hard_link_is_refused(self):
        self.populate()
        os.link(self.victim, self.root / 'home/hard')
        root_fd, _ = self.runtime_fds()
        with self.assertRaises(RuntimeError):
            vc.copy_durable(root_fd, self.data, ACCOUNT, UID)

    def test_directory_swapped_for_link_after_inventory_is_refused(self):
        self.populate()
        secret_dir = self.base / 'secret-dir'
        secret_dir.mkdir()
        (secret_dir / 'a.txt').write_text('secret')
        root_fd, _ = self.runtime_fds()
        dirs, files = vc.durable_inventory(root_fd, UID)
        target_fd = os.open(self.data, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, target_fd)
        for name in ('home', 'home/sub'):
            (self.data / name).mkdir(mode=0o700)
        # The sandbox swaps a validated directory for a link to somewhere else.
        (self.root / 'home/sub/a.txt').unlink(); (self.root / 'home/sub').rmdir()
        (self.root / 'home/sub').symlink_to(secret_dir)
        with self.assertRaises(RuntimeError):
            vc.copy_file(root_fd, target_fd, ('home', 'sub', 'a.txt'), 5, UID, ACCOUNT)
        self.assertFalse((self.data / 'home/sub/a.txt').exists())


class FailurePath(Base):
    def test_failed_migration_does_not_follow_planted_marker(self):
        (self.root / 'runtime/gateway-refresh-requested').symlink_to(self.victim)
        root_fd, runtime_fd = self.runtime_fds()
        rt = mock.Mock()
        rt.service_info.return_value = {'ActiveState': 'active'}
        with mock.patch.object(vc, 'desktop_account', return_value=None), \
             mock.patch.object(vc.os, 'kill'), \
             mock.patch.object(vc, 'process_state', return_value='T'), \
             mock.patch.object(vc, 'group_pids', return_value=[]), \
             mock.patch.object(vc, 'copy_durable', side_effect=RuntimeError('copy failed')):
            with self.assertRaises(RuntimeError):
                vc._migrate_locked(rt, ACCOUNT, 4242, UID, root_fd, runtime_fd)
        self.victim_untouched()


if __name__ == '__main__':
    unittest.main()
