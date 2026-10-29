"""Local beginner interface. Run python3 tutorial.py and open the printed loopback URL."""
from __future__ import annotations
import argparse
from collections import Counter
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit,parse_qs
import uuid
import serp_monitor as m

ROOT=Path(__file__).resolve().parent

class MeasuredClient(m.ApiClient):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs);self.api_attempts={"POST":0,"GET":0}
 def request(self,method,*args,**kwargs):
  self.api_attempts[method]=self.api_attempts.get(method,0)+1
  return super().request(method,*args,**kwargs)

class App:
 def __init__(self,root=ROOT,client_factory=MeasuredClient):
  self.root=Path(root).resolve();self.client_factory=client_factory
  self.db=self.root/'data/rankings.sqlite';self.config=self.root/'config.json';self.env=self.root/'.env'
  self.token=uuid.uuid4().hex;self.guard=threading.Lock();self.requests=set()
  self.state={'state':'ready','log':[],'rows':[],'run_id':None,'report':None}
  self.restore()
 def restore(self):
  if not self.db.exists():return
  try:
   with sqlite3.connect('file:'+str(self.db)+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    last=db.execute("SELECT id,status FROM runs WHERE mode='live' ORDER BY started_at DESC LIMIT 1").fetchone()
    if last:
     report=db.execute('SELECT rows_json FROM report_versions WHERE run_id=? ORDER BY version DESC LIMIT 1',(last['id'],)).fetchone()
     if report:self.state.update(state='complete' if last['status']=='success' else 'incomplete',rows=json.loads(report[0]),run_id=last['id'])
     files=sorted((self.root/'output').glob(last['id']+'-v*.csv'))
     if files:self.state['report']=str(files[-1].relative_to(self.root))
  except sqlite3.Error:self.state['state']='history_needs_review'
 def settings(self):
  config=m.load_config(self.config)
  return {'config':config,'key_is_set':bool(os.environ.get('SCRAPELESS_API_KEY') or self.env.exists()),**self.state}
 def save(self,data):
  if self.state['state']=='running':raise m.Busy('Wait for the current collection to finish.')
  config={'keywords':[s.strip() for s in data.get('keywords','').splitlines() if s.strip()],
   'target_domains':[data.get('target_domain','').strip()],
   'gl':data.get('gl','us'),'hl':data.get('hl','en'),'google_domain':data.get('google_domain','google.com'),
   'location':data.get('location','Austin, Texas, United States')}
  if len(config['keywords'])>20:raise m.MonitorError('Use at most 20 keywords in this beginner interface.')
  temp=self.root/('.config-'+uuid.uuid4().hex+'.json')
  try:
   temp.write_text(json.dumps(config,indent=2)+'\n');config=m.load_config(temp)
   key=data.get('api_key','').strip()
   if key:
    if any(c.isspace() for c in key) or key in {'YOUR_SCRAPELESS_API_KEY','YOUR_API_KEY'}:
     raise m.MonitorError('Paste your actual key, not the example placeholder.')
    env_temp=self.env.with_name('.env.'+uuid.uuid4().hex+'.tmp')
    env_temp.write_text('SCRAPELESS_API_KEY='+key+'\n');env_temp.chmod(0o600);os.replace(env_temp,self.env)
   elif not self.env.is_file() and not os.environ.get('SCRAPELESS_API_KEY'):
    raise m.MonitorError('Add your API key before saving.')
   os.replace(temp,self.config)
  finally:temp.unlink(missing_ok=True)
  return {'saved':True,'key_is_set':True}
 def start(self,request_id):
  if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):raise m.MonitorError('Reload the page and try again.')
  with self.guard:
   if request_id in self.requests:return {'accepted':True,'duplicate':True}
   if self.state['state']=='running':raise m.Busy('A collection is already running.')
   m.load_key(self.env);m.load_config(self.config)
   self.requests.add(request_id)
   self.state={'state':'running','log':['Starting collection. Keep this page open.'],'rows':[],'run_id':None,'report':None}
   threading.Thread(target=self.collect,daemon=True).start()
  return {'accepted':True}
 def collect(self):
  started=m.utc_now();client=None
  try:
   with m.database_lock(self.db):
    db=m.connect(self.db)
    try:
     config=m.load_config(self.config);key=m.load_key(self.env)
     rid=m.create_run(db,config,'live');self.state['run_id']=rid
     client=self.client_factory(key)
     success=m.run_collection(db,rid,client,self.state['log'].append)
     rows=m.report_rows(db,rid);revision=rows[0]['report_version']
     output=self.root/'output'/(rid+'-v'+str(revision)+'.csv');m.export_report(db,rid,output)
     summary={'run_id':rid,'started_at':started,'finished_at':m.utc_now(),'complete':success,
      'snapshot_counts':dict(Counter(row[0] for row in db.execute('SELECT status FROM snapshots WHERE run_id=?',(rid,)))),
      'report':str(output.relative_to(self.root)),'api_http_attempts':getattr(client,'api_attempts',{}),'google_redirect_lookups':getattr(client,'google_redirect_lookups',0),
      'scope':'Desktop; first returned organic page; positions and result counts can change.'}
     summary_path=output.with_suffix('.summary.json');summary_path.write_text(json.dumps(summary,indent=2)+'\n')
     self.state.update(state='complete' if success else 'incomplete',rows=rows,report=str(output.relative_to(self.root)),summary=summary)
     self.state['log'].append('CSV saved: '+str(output.relative_to(self.root)))
     self.state['log'].append('SQLite history saved: data/rankings.sqlite')
    finally:db.close()
  except (m.MonitorError,OSError,sqlite3.Error) as exc:
   self.state.update(state='incomplete',error=str(exc));self.state['log'].append(str(exc))
 def snapshots(self):
  if self.state['state']=='running':raise m.Busy('Wait for collection to finish before inspecting history.')
  if not self.db.exists():return []
  with sqlite3.connect('file:'+str(self.db)+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row
   return [{**{k:row[k] for k in ('id','run_id','keyword','status','submitted_at','received_at','baseline_id')},'organic_count':len(json.loads(row['organic_json'])) if row['organic_json'] else None} for row in db.execute('SELECT * FROM snapshots ORDER BY id')]

def make_server(app,port=8791):
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def send(self,status,body,content='application/json',download=None):
   raw=json.dumps(body).encode() if content=='application/json' else body.encode() if isinstance(body,str) else body
   self.send_response(status);self.send_header('Content-Type',content);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
   if download:self.send_header('Content-Disposition','attachment; filename="'+download+'"')
   self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  def host_valid(self):
   return self.headers.get('Host') in {'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)}
  def do_GET(self):
   if not self.host_valid():self.send(403,{'error':'Local access only.'});return
   path=urlsplit(self.path).path
   try:
    if path=='/':self.send(200,(ROOT/'starter.html').read_text().replace('__LOCAL_TOKEN__',app.token),'text/html; charset=utf-8')
    elif path=='/api/state':self.send(200,app.settings())
    elif path=='/api/snapshots':self.send(200,{'rows':app.snapshots()})
    elif path=='/download/report.csv':
     report=app.state.get('report')
     if not report:raise m.MonitorError('No report yet. Run collection first.')
     file=(app.root/report).resolve()
     if not file.is_relative_to(app.root/'output') or not file.is_file():raise m.MonitorError('Report unavailable.')
     self.send(200,file.read_bytes(),'text/csv; charset=utf-8',file.name)
    else:self.send(404,{'error':'Page not found.'})
   except m.Busy as exc:self.send(409,{'error':str(exc)})
   except (m.MonitorError,OSError,sqlite3.Error):self.send(400,{'error':'Could not read the local project data. Check the command window.'})
  def do_POST(self):
   expected='http://'+self.headers.get('Host','')
   token=self.headers.get('X-Local-Token','')
   if not self.host_valid() or self.headers.get('Origin')!=expected or not hmac.compare_digest(token,app.token):self.send(403,{'error':'Open the local project page and try again.'});return
   try:
    length=int(self.headers.get('Content-Length','0'))
    if not 0<length<=20000:raise m.MonitorError('Invalid form size.')
    data=json.loads(self.rfile.read(length));path=urlsplit(self.path).path
    if path=='/api/save':result=app.save(data)
    elif path=='/api/run':result=app.start(data.get('request_id'))
    else:self.send(404,{'error':'Page not found.'});return
    self.send(200,result)
   except m.Busy as exc:self.send(409,{'error':str(exc)})
   except (m.MonitorError,ValueError,TypeError,OSError) as exc:
    message=str(exc) if isinstance(exc,m.MonitorError) else 'Could not save the local form.'
    self.send(400,{'error':message})
 return ThreadingHTTPServer(('127.0.0.1',port),Handler)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8791);a=p.parse_args()
 os.umask(0o077);app=App();server=make_server(app,a.port)
 print('Open http://127.0.0.1:'+str(server.server_port)+' in your browser.',flush=True)
 print('This is the local project helper. Clicking Run sends real paid searches.',flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
if __name__=='__main__':main()
