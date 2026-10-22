import importlib.util, unittest, os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('candidate',ROOT/'desktop_apps.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
PROFILE=Path('/home/customer/.local/share/decrypt-browser/decrypt')
ARGV=[b'/usr/lib/chromium/chromium',b'--ozone-platform=wayland',b'--user-data-dir='+str(PROFILE).encode(),b'--class=decrypt-os',b'--app=http://127.0.0.1:8811']
class ProcessPath:
 def __init__(self,raw,uid,exe,leaf=''):self.raw,self.uid,self.exe,self.leaf=raw,uid,exe,leaf
 def __truediv__(self,leaf):return ProcessPath(self.raw,self.uid,self.exe,leaf)
 def stat(self):return SimpleNamespace(st_uid=self.uid)
 def read_bytes(self):return self.raw
 def resolve(self):return Path(self.exe)
CONFIG=app.resolve_app('decrypt')
class Compat(unittest.TestCase):
 def owns(self,raw,uid=None,exe='/usr/lib/chromium/chromium'):
  proc=ProcessPath(raw,os.getuid() if uid is None else uid,exe)
  with patch.object(app,'Path',side_effect=lambda p:proc if str(p)=='/proc' else Path(p)):
   return app.owns_process(1234,PROFILE,CONFIG)
 def test_normal_argv(self):self.assertTrue(self.owns(b'\0'.join(ARGV)+b'\0'))
 def test_native_flattened_argv(self):self.assertTrue(self.owns(b' '.join(ARGV)+b'\0'))
 def test_wrong_owner(self):self.assertFalse(self.owns(b' '.join(ARGV)+b'\0',uid=os.getuid()+1))
 def test_wrong_executable(self):self.assertFalse(self.owns(b' '.join(ARGV)+b'\0',exe='/usr/bin/python3'))
 def test_wrong_profile(self):self.assertFalse(self.owns((b' '.join(ARGV)+b'\0').replace(b'/decrypt-browser/decrypt',b'/decrypt-browser/other')))
 def test_wrong_url(self):self.assertFalse(self.owns((b' '.join(ARGV)+b'\0').replace(b':8811',b':8812')))
 def test_missing_url(self):self.assertFalse(self.owns(b' '.join(ARGV[:-1])+b'\0'))
 def test_duplicate_profile(self):self.assertFalse(self.owns(b' '.join(ARGV+[ARGV[2]])+b'\0'))
 def test_missing_terminator(self):self.assertFalse(self.owns(b' '.join(ARGV)))
 def test_oversize(self):self.assertFalse(self.owns(b'x'*65536+b'\0'))
 def test_bad_utf8(self):self.assertFalse(self.owns(b'\xff\0'))
 def test_bad_quotes(self):self.assertFalse(self.owns(b"'unmatched\0"))
 def test_alias_owned(self):
  c={'class':'chrome-127.0.0.1__-Default','mapped':True,'pid':1234,'address':'0x123'}
  with patch.object(app,'query',return_value=[c]),patch.object(app,'owns_process',return_value=True):self.assertEqual(app.windows('decrypt',PROFILE,CONFIG),[c])
 def test_alias_other_profile(self):
  c={'class':'chrome-127.0.0.1__-Default','mapped':True,'pid':1234,'address':'0x123'}
  with patch.object(app,'query',return_value=[c]),patch.object(app,'owns_process',return_value=False):self.assertEqual(app.windows('decrypt',PROFILE,CONFIG),[])
 def test_alias_not_for_tradingview(self):
  c={'class':'chrome-127.0.0.1__-Default','mapped':True,'pid':1234,'address':'0x123'}
  tradingview=app.resolve_app('tradingview')
  with patch.object(app,'query',return_value=[c]),patch.object(app,'owns_process',return_value=True):self.assertEqual(app.windows('tradingview',PROFILE.parent/'tradingview',tradingview),[])
if __name__=='__main__':unittest.main(verbosity=2)
