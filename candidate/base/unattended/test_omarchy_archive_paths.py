"""Archive path regression. No device, network, chroot or package operation."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from phase_b import omarchy_child, unpack_omarchy


class ArchivePathTests(unittest.TestCase):
    def test_actual_space_named_members_extract(self):
        names = ('applications/Disk Usage.desktop', 'applications/Google Contacts.desktop',
            'applications/Google Maps.desktop', 'applications/Google Messages.desktop',
            'applications/Google Photos.desktop', 'applications/icons/Disk Usage.png',
            'applications/icons/Google Contacts.png', 'applications/icons/Google Maps.png',
            'applications/icons/Google Messages.png', 'applications/icons/Google Photos.png',
            'applications/icons/Retro Gaming.png')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); archive = root / 'omarchy.tar'; destination = root / 'out'
            with tarfile.open(archive, 'w') as output:
                for name in ('bin', 'config', 'default', 'applications', 'applications/icons'):
                    row = tarfile.TarInfo(name); row.type = tarfile.DIRTYPE; output.addfile(row)
                for name in names:
                    row = tarfile.TarInfo(name); row.size = 2; row.mode = 0o644; output.addfile(row, io.BytesIO(b'ok'))
            unpack_omarchy(archive, destination)
            self.assertTrue(all((destination / name).read_bytes() == b'ok' for name in names))

    def test_traversal_and_nonordinary_whitespace_remain_refused(self):
        for name in ('', '/root', '../escape', 'a/../escape', 'a/./x', 'a//x', 'a\\x',
                     'a\tb', 'a\nb', 'a\x00b', 'a\x7fb', 'a\u00a0b'):
            with self.subTest(name=repr(name)), self.assertRaises(common.Refused):
                omarchy_child(Path('/root'), name)

    def test_common_signed_media_paths_are_still_strict(self):
        with self.assertRaises(ValueError): common.child(Path('/root'), 'applications/Disk Usage.desktop')


if __name__ == '__main__': unittest.main()
