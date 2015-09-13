"""Unit tests for flickr.py.
"""

__author__ = ['Kyle Mahan <kyle@kylewm.com>']

import json
import urllib

import appengine_config
import flickr
import granary
import granary.test.test_flickr
import models
import oauth_dropins
import tasks
import testutil


class FlickrTest(testutil.ModelsTest):

  def setUp(self):
    super(FlickrTest, self).setUp()
    oauth_dropins.appengine_config.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.FLICKR_APP_SECRET = 'my_app_secret'

    self.auth_entity = oauth_dropins.flickr.FlickrAuth(
      id='my_string_id',
      token_key='my_key', token_secret='my_secret',
      user_json=json.dumps(granary.test.test_flickr.PERSON_INFO))

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
                        + urllib.urlencode(full_params), result)

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

    with self.assertRaises(models.DisableSource):
      poll_task = tasks.Poll()
      poll_task.poll(self.flickr)
