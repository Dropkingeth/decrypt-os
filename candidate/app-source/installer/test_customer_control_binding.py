#!/usr/bin/env python3
"""Positive first-start authorization: producer/consumer joins and fail-closed mutations."""
import copy,importlib.util,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
HERE=Path(__file__).resolve().parent
m=load('control',HERE/'customer_control.py')
f=load('firstboot',HERE.parents[1]/'firstboot/firstboot.py')
class BindingTests(unittest.TestCase):
 def setUp(self):
  self.machine='a'*32
  self.state={'schema':'decrypt.firstboot-state.v1','instanceId':'11111111-2222-4333-8444-555555555555','imageId':'fixture-image','policySha256':'b'*64,'phase':'notice','status':'pending','accountName':'customer','seq':4,'failure':None,'completedAt':None,'notice':{'documentSha256':'c'*64,'accepted':False,'acceptedAt':None}}
  self.documents=f.authorization_documents(self.state,self.machine,SimpleNamespace(pw_name='customer',pw_uid=1000),b'{}')
 def validate(self,docs=None,state=None,machine=None):
  p,e,r=docs or self.documents
  return m.validate_instance(p,state or self.state,e,r,machine or self.machine)
 def test_actual_producer_is_accepted_pending_and_complete(self):
  self.validate()
  state=copy.deepcopy(self.state);state.update(phase='complete',status='complete',completedAt='2021-09-29T00:00:00+00:00');state['notice'].update(accepted=True,acceptedAt=state['completedAt'])
  self.validate(state=state)
 def test_other_machine_missing_state_and_cross_instance_refused(self):
  with self.assertRaises(ValueError):self.validate(machine='d'*32)
  for key,value in [('instanceId','66666666-7777-4888-9999-aaaaaaaaaaaa'),('imageId','other-image'),('accountName','other'),('policySha256','d'*64),('phase','identity'),('failure',{'code':'failed'})]:
   state=copy.deepcopy(self.state);state[key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):self.validate(state=state)
  with self.assertRaises(ValueError):m.validate_instance(self.documents[0],{},self.documents[1],self.documents[2],self.machine)
 def test_marker_receipt_policy_joins_refuse_drift(self):
  for position in range(3):
   for key in ('instanceId','imageId','sourceManifestSha256','desktopUid'):
    docs=copy.deepcopy(self.documents);docs[position][key]='wrong'
    with self.subTest(position=position,key=key),self.assertRaises(ValueError):self.validate(docs)
 def test_no_authorization_before_identity_account_or_zero_machine(self):
  for state,machine in [(dict(self.state,phase='identity'),'a'*32),(dict(self.state,instanceId=None),'a'*32),(self.state,'0'*32)]:
   with self.assertRaises(ValueError):f.authorization_documents(state,machine,SimpleNamespace(pw_name='customer',pw_uid=1000),b'{}')
 def test_unsafe_owner_refused(self):
  with patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=0o100644,st_uid=1000)):
   with self.assertRaises(ValueError):m.trusted_file(Path('/fixture/policy.json'))
if __name__=='__main__':unittest.main()
