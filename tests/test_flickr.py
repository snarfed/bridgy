"""Unit tests for flickr.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
import urllib.request, urllib.parse, urllib.error

import appengine_config
import flickr
import granary
import granary.tests.test_flickr as gr_test_flickr
import oauth_dropins
import tasks
import ujson as json

from . import testutil


class FlickrTest(testutil.ModelsTest):

  def setUp(self):
    super(FlickrTest, self).setUp()
    oauth_dropins.appengine_config.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.FLICKR_APP_SECRET = 'my_app_secret'

    self.auth_entity = oauth_dropins.flickr.FlickrAuth(
      id='my_string_id',
      token_key='my_key', token_secret='my_secret',
      user_json=json.dumps(gr_test_flickr.PERSON_INFO))

    self.auth_entity.put()
    self.flickr = flickr.Flickr.new(self.handler, self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.flickr.auth_entity.get())
    self.assertEqual('39216764@N00', self.flickr.key.id())
    self.assertEqual('Kyle Mahan', self.flickr.name)
    self.assertEqual('kindofblue115', self.flickr.username)
    self.assertEqual('https://www.flickr.com/people/kindofblue115/',
                     self.flickr.silo_url())
    self.assertEqual('tag:flickr.com,2013:kindofblue115', self.flickr.user_tag_id())

  def expect_call_api_method(self, method, params, result):
    # FIXME duplicated from granary.test_flickr.FlickrTest, not sure
    # how to share
    full_params = {
      'nojsoncallback': 1,
      'format': 'json',
      'method': method,
    }
    full_params.update(params)
    self.expect_urlopen('https://api.flickr.com/services/rest?'
                        + urllib.parse.urlencode(full_params), result)

  def test_revoked_disables_source(self):
    """ Make sure polling Flickr with a revoked token will
    disable it as a source.
    """
    self.expect_call_api_method('flickr.people.getPhotos', {
      'extras': granary.flickr.Flickr.API_EXTRAS,
      'per_page': 50,
      'user_id': 'me',
    }, json.dumps({
      'stat': 'fail',
      'code': 98,
      'message': 'Invalid auth token',
    }))
    self.mox.ReplayAll()

    self.flickr.features = ['listen']
    self.flickr.put()
    self.assertEqual('enabled', self.flickr.status)
    tasks.application.get_response(
      '/_ah/queue/poll', method='POST', body=native_str(urllib.parse.urlencode({
        'source_key': self.flickr.key.urlsafe(),
        'last_polled': '1970-01-01-00-00-00',
      })))
    self.assertEqual('disabled', self.flickr.key.get().status)

  @staticmethod
  def prepare_person_tags():
    flickr.Flickr(id='555', username='username').put()
    flickr.Flickr(id='666', domains=['my.domain']).put()
    input_urls = (
      'https://unknown/',
      'https://www.flickr.com/photos/444/',
      'https://flickr.com/people/444/',
      'https://flickr.com/photos/username/',
      'https://www.flickr.com/people/username/',
      'https://my.domain/',
    )
    expected_urls = (
      'https://unknown/',
      'https://www.flickr.com/photos/444/',
      'https://flickr.com/people/444/',
      'https://flickr.com/photos/username/',
      'https://www.flickr.com/people/username/',
      'https://www.flickr.com/people/666/',
    )
    return input_urls, expected_urls

  def test_preprocess_for_publish(self):
    input_urls, expected_urls = self.prepare_person_tags()
    activity = {
      'object': {
        'objectType': 'note',
        'content': 'a msg',
        'tags': [{'objectType': 'person', 'url': url} for url in input_urls],
      },
    }
    self.flickr.preprocess_for_publish(activity)
    self.assert_equals(expected_urls, [t['url'] for t in activity['object']['tags']])

  def test_canonicalize_url(self):
    def check(expected, url):
      for input in expected, url:
        self.assertEquals(expected, self.flickr.canonicalize_url(input))

    check('https://www.flickr.com/photos/xyz/123/',
          'http://flickr.com/photos/xyz/123')
    check('https://www.flickr.com/photos/xyz/123/',
          'https://www.flickr.com/photos/xyz/123')
    check('https://www.flickr.com/people/xyz/',
          'http://flickr.com/people/xyz')

    self.flickr.username = 'mee'
    check('https://www.flickr.com/photos/39216764@N00/123/',
          'http://flickr.com/photos/mee/123')
    check('https://www.flickr.com/people/39216764@N00/',
          'http://flickr.com/people/mee')

    self.assertIsNone(self.flickr.canonicalize_url(
      'https://login.yahoo.com/config/login?...'))
