"""Unit tests for build/generate_package_lock.py (no root, no VM)."""
import importlib.util
import io
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('generate_package_lock', HERE / 'generate_package_lock.py')
gpl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpl)

DESC = (
    '%FILENAME%\nacl-2.4.0-1-aarch64.pkg.tar.xz\n\n'
    '%NAME%\nacl\n\n'
    '%VERSION%\n2.4.0-1\n\n'
    '%CSIZE%\n143708\n\n'
    '%SHA256SUM%\n8d400d781a3fd765f0f7c98183492850f830da3d3efb05518b5927b446387350\n\n'
    '%ARCH%\naarch64\n'
)


def test_parse_desc_reads_pacman_repo_fields():
    fields = gpl.parse_desc(DESC)
    assert fields['NAME'] == ['acl']
    assert fields['VERSION'] == ['2.4.0-1']
    assert fields['SHA256SUM'] == ['8d400d781a3fd765f0f7c98183492850f830da3d3efb05518b5927b446387350']


def test_load_packages_from_repo_db(tmp_path):
    (tmp_path / 'pkg').mkdir()
    db = tmp_path / 'pkg/decrypt-offline.db.tar.gz'
    with tarfile.open(db, 'w:gz') as archive:
        data = DESC.encode()
        info = tarfile.TarInfo('acl-2.4.0-1/desc')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    packages = gpl.load_packages(tmp_path / 'pkg')
    assert packages == [{
        'name': 'acl', 'version': '2.4.0-1', 'filename': 'acl-2.4.0-1-aarch64.pkg.tar.xz',
        'sha256': '8d400d781a3fd765f0f7c98183492850f830da3d3efb05518b5927b446387350', 'bytes': 143708,
    }]


def test_load_packages_sorted_by_name(tmp_path):
    (tmp_path / 'pkg').mkdir()
    db = tmp_path / 'pkg/decrypt-offline.db.tar.gz'
    second = DESC.replace('acl', 'zzz-pkg')
    with tarfile.open(db, 'w:gz') as archive:
        for name, raw in (('acl-2.4.0-1/desc', DESC), ('zzz-pkg-2.4.0-1/desc', second)):
            data = raw.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    packages = gpl.load_packages(tmp_path / 'pkg')
    assert [row['name'] for row in packages] == ['acl', 'zzz-pkg']
