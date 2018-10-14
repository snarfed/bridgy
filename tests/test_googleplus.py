"""Unit tests for googleplus.py.
"""
from __future__ import unicode_literals

import json

import appengine_config
from apiclient import discovery
from apiclient import http
from granary.tests import test_googleplus as gr_test_googleplus
from oauth_dropins import googleplus as oauth_googleplus

from googleplus import GooglePlusPage
import testutil


class GooglePlusTest(testutil.ModelsTest):

  def setUp(self):
    super(GooglePlusTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_googleplus.GooglePlusAuth(
      id='my_string_id',
      creds_json=gr_test_googleplus.CREDS_JSON,
      user_json=json.dumps({'id': '987',
                            'displayName': 'Mr. G P',
                            'url': 'http://mr/g/p',
                            'image': {'url': 'http://pi.ct/ure?sz=50'},
                            }))
    self.auth_entity.put()
    self.gp = GooglePlusPage.new(self.handler, auth_entity=self.auth_entity)

  def tearDown(self):
    oauth_googleplus.json_service = None

  def test_new(self):
    self.assertEqual(self.auth_entity, self.gp.auth_entity.get())
    self.assertEqual('987', self.gp.key.string_id())
    self.assertEqual('http://pi.ct/ure?sz=50&sz=128', self.gp.picture)  # overridden sz
    self.assertEqual('Mr. G P', self.gp.name)
    self.assertEqual('http://mr/g/p', self.gp.url)
    self.assertEqual('http://mr/g/p', self.gp.silo_url())
    self.assertEqual('tag:plus.google.com,2013:987', self.gp.user_tag_id())

  def test_search_for_links(self):
    # should only search for urls without paths
    for urls in [], [], ['http://a/b'], ['https://c/d/e', 'https://f.com/g']:
      self.gp.domain_urls = urls
      self.assertEqual([], self.gp.search_for_links())

    # TODO: actually check search query. (still haven't figured out how with
    # RequestMockBuilder etc. :/ see granary/tests/test_googleplus.py for more.)
    self.gp.domain_urls = ['http://a', 'https://b/', 'http://c/d/e']
    oauth_googleplus.json_service = discovery.build_from_document(
      gr_test_googleplus.DISCOVERY_DOC, requestBuilder=http.RequestMockBuilder({
        'plus.activities.search':
          (None, json.dumps({'items': [gr_test_googleplus.ACTIVITY_GP]})),
      }))
    self.assertEqual([gr_test_googleplus.ACTIVITY_AS], self.gp.search_for_links())

  def test_canonicalize_url(self):
      for input, expected in (
          ('https://plus.google.com/103651231634018158746/posts/JwiJAfBNs9w/',
           'https://plus.google.com/103651231634018158746/posts/JwiJAfBNs9w'),
      ):
        self.assertEquals(expected, self.gp.canonicalize_url(input))
