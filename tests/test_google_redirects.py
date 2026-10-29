"""Google wrapper resolution never requests destination sites or sends API keys."""
import io,json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError,URLError
import serp_monitor as m
from test_monitor import config

class TestGoogleRedirects(unittest.TestCase):
 def error(self,location,status=302):
  return HTTPError('https://www.google.com/goto?url=EXAMPLE_OPAQUE',status,'Redirect',{'Location':location} if location else {},io.BytesIO())
 def test_opaque_relative_goto_resolves_without_key_or_destination_fetch(self):
  c=m.ApiClient('EXAMPLE_PRIVATE_TOKEN')
  with patch.object(c.redirect_opener,'open',side_effect=self.error('https://example.com/page')) as request:
   rows=m.parse_organic({'organic_results':[{'position':1,'link':'/goto?url=EXAMPLE_OPAQUE'}]},c.resolve_link)
  self.assertEqual(rows[0]['link'],'https://example.com/page')
  self.assertEqual(rows[0]['source_link'],'/goto?url=EXAMPLE_OPAQUE')
  req=request.call_args.args[0]
  self.assertEqual(req.full_url,'https://www.google.com/goto?url=EXAMPLE_OPAQUE')
  self.assertIsNone(req.get_header('X-api-token'))
  self.assertNotIn('EXAMPLE_PRIVATE_TOKEN',str(req.headers));self.assertEqual(request.call_count,1)
 def test_clear_target_is_decoded_locally(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open') as request:
   self.assertEqual(c.resolve_link('/url?q=https%3A%2F%2Fexample.com%2Fpage'),'https://example.com/page');request.assert_not_called()
 def test_unknown_relative_path_never_triggers_network(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open') as request:
   with self.assertRaises(m.MonitorError):m.parse_organic({'organic_results':[{'position':1,'link':'/unknown'}]},c.resolve_link)
   request.assert_not_called()
 def test_arbitrary_absolute_destination_is_not_fetched(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open') as request:
   self.assertEqual(c.resolve_link('https://example.com/goto?url=opaque'),'https://example.com/goto?url=opaque');request.assert_not_called()
 def test_invalid_or_missing_location_remains_error(self):
  for location,status in [('javascript:alert(1)',302),('https://user:password@example.com',302),(None,302),(None,429)]:
   c=m.ApiClient('dummy')
   with patch.object(c.redirect_opener,'open',side_effect=self.error(location,status)):
    with self.assertRaises(m.MonitorError):c.resolve_link('/goto?url=EXAMPLE_OPAQUE')
 def test_network_failure_does_not_repeat_search(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open',side_effect=URLError('controlled')) as request:
   with self.assertRaises(m.MonitorError):c.resolve_link('/goto?url=EXAMPLE_OPAQUE')
  self.assertEqual(request.call_count,1)
 def test_google_chain_is_bounded(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open',side_effect=self.error('/goto?url=EXAMPLE_NEXT')) as request:
   with self.assertRaises(m.MonitorError):c.resolve_link('/goto?url=EXAMPLE_OPAQUE')
  self.assertEqual(request.call_count,3)
 def test_cached_redirect_only_looked_up_once(self):
  c=m.ApiClient('dummy')
  with patch.object(c.redirect_opener,'open',side_effect=self.error('https://example.com/page')) as request:
   self.assertEqual(c.resolve_link('/goto?url=EXAMPLE_OPAQUE'),c.resolve_link('/goto?url=EXAMPLE_OPAQUE'))
  self.assertEqual(request.call_count,1)
 def test_storage_matches_resolved_domain_and_retains_provenance(self):
  c=m.ApiClient('dummy');db=m.connect(Path(':memory:'));run=m.create_run(db,config(),'live')
  try:
   with patch.object(c,'search',return_value={'organic_results':[{'position':2,'link':'/goto?url=EXAMPLE_OPAQUE'}]}),patch.object(c.redirect_opener,'open',side_effect=self.error('https://example.com/page')):
    self.assertTrue(m.run_collection(db,run,c,lambda _:None))
   self.assertEqual(m.report_rows(db,run)[0]['current_rank'],2)
   snap=db.execute('SELECT organic_json,diagnostic_json FROM snapshots').fetchone()
   self.assertEqual(json.loads(snap['organic_json'])[0]['link_source'],'google_redirect_location')
   self.assertEqual(json.loads(snap['diagnostic_json'])['invalid_item_indexes'],[])
   self.assertEqual(json.loads(snap['diagnostic_json'])['resolved_google_redirect_indexes'],[0])
  finally:db.close()
