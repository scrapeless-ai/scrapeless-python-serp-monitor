"""The local beginner form writes private settings and uses the real core workflow."""
import csv,json,os
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import tutorial
import serp_monitor as m

class FixtureClient:
 def __init__(self,key):self.api_attempts={'POST':0,'GET':0};self.google_redirect_lookups=0
 def search(self,data,on_task,task_id=None):
  self.api_attempts['POST']+=1
  return {'organic_results':[{'position':2,'link':'https://scrapeless.com/en'}]}

class TestBeginnerInterface(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.config={'keywords':['scrapeless','web scraping api','google search api'],'target_domains':['scrapeless.com'],'gl':'us','hl':'en','google_domain':'google.com','location':'Austin, Texas, United States'}
  (self.root/'config.json').write_text(json.dumps(self.config))
  self.app=tutorial.App(self.root,FixtureClient)
 def tearDown(self):self.temp.cleanup()
 def form(self,key='EXAMPLE_TEST_ONLY_KEY'):
  return {'api_key':key,'keywords':'\n'.join(self.config['keywords']),'target_domain':'scrapeless.com','gl':'us','hl':'en','google_domain':'google.com','location':self.config['location']}
 def test_key_is_private_and_never_returned_by_state(self):
  self.app.save(self.form());state=self.app.settings()
  self.assertTrue(state['key_is_set']);self.assertNotIn('EXAMPLE_TEST_ONLY_KEY',json.dumps(state))
  self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)
 def test_placeholder_cannot_be_saved_as_working_key(self):
  with self.assertRaises(m.MonitorError):self.app.save(self.form('YOUR_SCRAPELESS_API_KEY'))
  self.assertFalse((self.root/'.env').exists())
 def test_blank_key_preserves_saved_key(self):
  self.app.save(self.form());before=(self.root/'.env').read_bytes();self.app.save(self.form(''))
  self.assertEqual((self.root/'.env').read_bytes(),before)
 def test_ui_workflow_produces_three_sqlite_snapshots_and_csv_rows(self):
  self.app.save(self.form());self.app.start('a'*32)
  end=time.monotonic()+5
  while self.app.state['state']=='running' and time.monotonic()<end:time.sleep(.01)
  self.assertEqual(self.app.state['state'],'complete')
  self.assertEqual(len(self.app.snapshots()),3)
  self.assertEqual(self.app.state['summary']['api_http_attempts'],{'POST':3,'GET':0})
  with (self.root/self.app.state['report']).open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
  self.assertEqual(len(rows),3);self.assertTrue(all(r['status']=='baseline' for r in rows))
  before=self.app.state['run_id'];self.assertTrue(self.app.start('a'*32)['duplicate']);self.assertEqual(self.app.state['run_id'],before)
  restored=tutorial.App(self.root,FixtureClient);self.assertEqual(restored.state['run_id'],before)
  self.assertEqual(len(restored.state['rows']),3)
 def test_busy_form_cannot_change_inputs_mid_run(self):
  self.app.state['state']='running'
  with self.assertRaises(m.Busy):self.app.save(self.form())
 def test_invalid_request_id_cannot_start_collection(self):
  with self.assertRaises(m.MonitorError):self.app.start('not-an-id')
 def test_http_guard_and_real_saved_state(self):
  server=tutorial.make_server(self.app,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  origin='http://127.0.0.1:'+str(server.server_port)
  try:
   with urlopen(origin+'/') as r:page=r.read().decode()
   self.assertIn('Local Python project',page);self.assertNotIn('__LOCAL_TOKEN__',page)
   body=json.dumps(self.form()).encode()
   bad=Request(origin+'/api/save',data=body,headers={'Origin':origin,'Content-Type':'application/json','X-Local-Token':'wrong'})
   with self.assertRaises(HTTPError) as caught:urlopen(bad)
   self.assertEqual(caught.exception.code,403);self.assertFalse((self.root/'.env').exists())
   good=Request(origin+'/api/save',data=body,headers={'Origin':origin,'Content-Type':'application/json','X-Local-Token':self.app.token})
   with urlopen(good) as r:self.assertTrue(json.loads(r.read())['saved'])
   with urlopen(origin+'/api/state') as r:state=r.read().decode()
   self.assertNotIn('EXAMPLE_TEST_ONLY_KEY',state)
   foreign=Request(origin+'/api/state',headers={'Host':'external.invalid'})
   with self.assertRaises(HTTPError) as caught:urlopen(foreign)
   self.assertEqual(caught.exception.code,403)
  finally:server.shutdown();server.server_close();thread.join()
