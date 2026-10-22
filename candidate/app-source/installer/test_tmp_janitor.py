import os
import tempfile
import time
import unittest
from pathlib import Path

import tmp_janitor as janitor


class PlanTests(unittest.TestCase):
    def entry(self, name, age_seconds, uid=1000, own_uid=1000, is_symlink=False):
        return {'name': name, 'is_symlink': is_symlink, 'uid': uid, 'own_uid': own_uid,
                'mtime': 1000.0 - age_seconds}

    def test_young_items_are_left_alone(self):
        entries = [self.entry('chromium-abc', age_seconds=60)]
        self.assertEqual(janitor.plan(entries, now=1000.0), [])

    def test_old_disposable_cache_is_deleted(self):
        entries = [self.entry('chromium-abc', age_seconds=janitor.THRESHOLD_SECONDS + 1)]
        self.assertEqual(janitor.plan(entries, now=1000.0), [('chromium-abc', 'delete')])

    def test_old_unknown_item_is_archived_not_deleted(self):
        entries = [self.entry('some-report.json', age_seconds=janitor.THRESHOLD_SECONDS + 1)]
        self.assertEqual(janitor.plan(entries, now=1000.0), [('some-report.json', 'archive')])

    def test_items_owned_by_another_account_are_never_touched(self):
        entries = [self.entry('chromium-abc', age_seconds=janitor.THRESHOLD_SECONDS + 1, uid=0)]
        self.assertEqual(janitor.plan(entries, now=1000.0), [])

    def test_symlinks_are_never_touched(self):
        entries = [self.entry('chromium-abc', age_seconds=janitor.THRESHOLD_SECONDS + 1, is_symlink=True)]
        self.assertEqual(janitor.plan(entries, now=1000.0), [])


class ApplyTests(unittest.TestCase):
    def test_disposable_deleted_unknown_archived_young_kept(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            tmp, home = Path(tmp), Path(home)
            old = time.time() - janitor.THRESHOLD_SECONDS - 10
            (tmp / 'chromium-old').mkdir(); os.utime(tmp / 'chromium-old', (old, old))
            (tmp / 'unknown-old.json').write_text('{}'); os.utime(tmp / 'unknown-old.json', (old, old))
            (tmp / 'fresh-report.json').write_text('{}')
            result = janitor.apply(tmp_dir=tmp, home=home)
            self.assertEqual(result['failed'], [])
            self.assertEqual(result['deleted'], ['chromium-old'])
            self.assertEqual(result['archived'], ['unknown-old.json'])
            self.assertFalse((tmp / 'chromium-old').exists())
            self.assertFalse((tmp / 'unknown-old.json').exists())
            self.assertTrue((tmp / 'fresh-report.json').exists())
            self.assertTrue((home / janitor.ARCHIVE_RELATIVE / 'unknown-old.json').exists())

    def test_name_collision_in_archive_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            tmp, home = Path(tmp), Path(home)
            old = time.time() - janitor.THRESHOLD_SECONDS - 10
            archive_dir = home / janitor.ARCHIVE_RELATIVE
            archive_dir.mkdir(parents=True)
            (archive_dir / 'unknown-old.json').write_text('{"kept":true}')
            (tmp / 'unknown-old.json').write_text('{"new":true}'); os.utime(tmp / 'unknown-old.json', (old, old))
            result = janitor.apply(tmp_dir=tmp, home=home, now=old + janitor.THRESHOLD_SECONDS + 10)
            self.assertEqual(result['failed'], [])
            self.assertEqual((archive_dir / 'unknown-old.json').read_text(), '{"kept":true}')
            self.assertFalse((tmp / 'unknown-old.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
