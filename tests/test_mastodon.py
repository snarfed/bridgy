"""Unit tests for mastodon.py.
"""
from granary.mastodon import API_BLOCKS, API_SEARCH
from granary.tests.test_mastodon import STATUS
from oauth_dropins import mastodon as oauth_mastodon
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import json_dumps, json_loads

from . import testutil
from mastodon import Mastodon


class MastodonTest(testutil.AppTest):

  def setUp(self):
    super().setUp()

    app = oauth_mastodon.MastodonApp(instance='https://foo.com', data='')
    app.put()
    self.auth_entity = oauth_mastodon.MastodonAuth(
      id='@ME@foo.com', access_token_str='towkin', app=app.key, user_json=json_dumps({
        'id': '123',
        'username': 'ME',
        'acct': 'ME',
        'url': 'https://foo.com/@ME',
        'display_name': 'Ryan Barrett',
        'avatar': 'http://pi.ct/ure',
      }))
    self.auth_entity.put()
    self.m = Mastodon.new(auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.m.auth_entity.get())
    self.assertEqual('towkin', self.m.gr_source.access_token)
    self.assertEqual('@me@foo.com', self.m.key.string_id())
    self.assertEqual('http://pi.ct/ure', self.m.picture)
    self.assertEqual('Ryan Barrett', self.m.name)
    self.assertEqual('https://foo.com/@ME', self.m.url)
    self.assertEqual('https://foo.com/@ME', self.m.silo_url())
    self.assertEqual('tag:foo.com,2013:me', self.m.user_tag_id())
    self.assertEqual('@ME@foo.com (Mastodon)', self.m.label())

  def test_canonicalize_url(self):
    good = 'https://foo.com/@x/123'
    self.assertEqual(good, self.m.canonicalize_url(good))
    self.assertEqual(good, self.m.canonicalize_url('http://foo.com/@x/123/'))

  def test_is_private(self):
    self.assertFalse(self.m.is_private())

    self.auth_entity.user_json = json_dumps({'locked': True})
    self.auth_entity.put()
    self.assertTrue(self.m.is_private())

  def test_load_blocklist_missing_scope(self):
    self.expect_requests_get('https://foo.com' + API_BLOCKS,
                             headers={'Authorization': 'Bearer towkin'},
                             status_code=403)
    self.mox.ReplayAll()
    self.m.load_blocklist()
    self.assertEqual([], self.m.blocked_ids)
    self.assertFalse(self.m.is_blocked({'numeric_id': 123}))

  def test_gr_class_with_max_toot_chars(self):
    app = self.auth_entity.app.get()
    app.instance_info = '{"max_toot_chars": 999}'
    app.put()
    self.assert_equals(999, self.m.gr_source.TRUNCATE_TEXT_LENGTH)
