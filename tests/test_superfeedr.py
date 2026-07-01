"""Unit tests for superfeedr.py."""
from flask import Flask
from google.cloud.ndb.key import _MAX_KEYPART_BYTES
from google.cloud.ndb._datastore_types import _MAX_STRING_LENGTH
from webutil.testutil import requests_response
from webutil.util import json_dumps

from models import BlogPost
import superfeedr
from . import testutil


class FakeNotify(superfeedr.Notify):
  SOURCE_CLS = testutil.FakeSource


class SuperfeedrTest(testutil.AppTest):

  def setUp(self):
    super().setUp()

    self.app = Flask('test_superfeedr')
    self.app.add_url_rule('/notify/<id>', methods=['POST'],
                          view_func=FakeNotify.as_view('test_superfeedr'))
    self.app.config['ENV'] = 'development'
    self.client = self.app.test_client()

    self.source = testutil.FakeSource(id='foo.com', domains=['foo.com'],
                                      features=['webmention'])
    self.source.put()
    self.item = {'id': 'A', 'content': 'B'}
    self.feed = {'items': [self.item]}

  def assert_blogposts(self, expected):
    got = list(BlogPost.query())
    self.assert_entities_equal(expected, got, ignore=('created', 'updated'))

  def test_subscribe(self):
    expected_data = {
      'hub.mode': 'subscribe',
      'hub.topic': 'fake feed url',
      'hub.callback': 'http://localhost/fake/notify/foo.com',
      'format': 'json',
      'retrieve': 'true',
    }
    item_a = {'permalinkUrl': 'A', 'content': 'a http://a.com a'}
    item_b = {'permalinkUrl': 'B', 'summary': 'b http://b.com b'}
    feed = {'items': [item_a, {}, item_b]}
    self.mock_post.return_value = requests_response(feed)

    post_a = BlogPost(id='A', source=self.source.key, feed_item=item_a,
                      unsent=['http://a.com/'])
    post_b = BlogPost(id='B', source=self.source.key, feed_item=item_b,
                      unsent=['http://b.com/'])

    with self.app.test_request_context():
      superfeedr.subscribe(self.source)
      self.assert_blogposts([post_a, post_b])
    self.assert_requests_post(superfeedr.PUSH_API_URL, data=expected_data)
    self.assert_tasks({'queue': 'propagate-blogpost', 'key': post_a},
                      {'queue': 'propagate-blogpost', 'key': post_b})

  def test_handle_feed(self):
    item_a = {'permalinkUrl': 'A',
              'content': 'a http://a.com http://foo.com/self/link b'}
    post_a = BlogPost(id='A', source=self.source.key, feed_item=item_a,
                      # self link should be discarded
                      unsent=['http://a.com/'])

    superfeedr.handle_feed({'items': [item_a]}, self.source)
    self.assert_blogposts([post_a])
    self.assert_task('propagate-blogpost', key=post_a)

  def test_handle_feed_no_items(self):
    superfeedr.handle_feed({}, self.source)
    self.assert_blogposts([])

    superfeedr.handle_feed(None, self.source)
    self.assert_blogposts([])

  def test_handle_feed_disabled_source(self):
    self.source.status = 'disabled'
    self.source.put()
    superfeedr.handle_feed(self.feed, self.source)
    self.assert_blogposts([])

  def test_handle_feed_source_missing_webmention_feature(self):
    self.source.features = ['listen']
    self.source.put()
    superfeedr.handle_feed(self.feed, self.source)
    self.assert_blogposts([])

  def test_handle_feed_allows_bridgy_publish_links(self):
    item = {'permalinkUrl': 'A', 'content': 'a https://brid.gy/publish/twitter b'}

    superfeedr.handle_feed({'items': [item]}, self.source)
    self.assert_equals(['https://brid.gy/publish/twitter'],
                       BlogPost.get_by_id('A').unsent)
    self.assert_task('propagate-blogpost', key=BlogPost(id='A'))

  def test_handle_feed_unwraps_t_umblr_com_links(self):
    item = {
      'permalinkUrl': 'A',
      'id': 'A',
      'content': 'x <a href="http://t.umblr.com/redirect?z=http%3A%2F%2Fwrap%2Fped&amp;t=YmZkMzQy..."></a> y',
    }
    post = BlogPost(id='A', source=self.source.key, feed_item=item,
                    unsent=['http://wrap/ped'])

    superfeedr.handle_feed({'items': [item]}, self.source)
    self.assert_blogposts([post])
    self.assert_task('propagate-blogpost', key=post)

  def test_handle_feed_cleans_links(self):
    item = {
      'permalinkUrl': 'A',
      'id': 'A',
      'content': 'x <a href="http://abc?source=rss----12b80d28f892---4',
    }
    post = BlogPost(id='A', source=self.source.key, feed_item=item,
                    unsent=['http://abc/'])

    superfeedr.handle_feed({'items': [item]}, self.source)
    self.assert_blogposts([post])
    self.assert_task('propagate-blogpost', key=post)

  def test_notify_view(self):
    item = {'id': 'X', 'content': 'a http://x/y z'}
    post = BlogPost(id='X', source=self.source.key, feed_item=item,
                    unsent=['http://x/y'])

    resp = self.client.post('/notify/foo.com', json={'items': [item]})
    self.assertEqual(200, resp.status_code)
    self.assert_blogposts([post])
    self.assert_task('propagate-blogpost', key=post)

  def test_notify_url_too_long(self):
    item = {'id': 'X' * (_MAX_KEYPART_BYTES + 1), 'content': 'a http://x/y z'}
    resp = self.client.post('/notify/foo.com', json={'items': [item]})

    self.assertEqual(200, resp.status_code)
    self.assert_blogposts([BlogPost(id='X' * _MAX_KEYPART_BYTES,
                                    source=self.source.key, feed_item=item,
                                    failed=['http://x/y'], status='complete')])

  def test_notify_link_too_long(self):
    too_long = 'http://a/' + 'b' * _MAX_STRING_LENGTH
    item = {'id': 'X', 'content': f'a http://x/y {too_long} z'}
    post = BlogPost(id='X', source=self.source.key, feed_item=item,
                    unsent=['http://x/y'], status='new')

    resp = self.client.post('/notify/foo.com', json={'items': [item]})
    self.assertEqual(200, resp.status_code)
    self.assert_blogposts([post])
    self.assert_task('propagate-blogpost', key=post)

  def test_notify_utf8(self):
    """Check that we handle unicode chars in content ok, including logging."""
    self.feed = {'items': [{'id': 'X', 'content': 'a ☕ z'}]}
    resp = self.client.post('/notify/foo.com', json=self.feed)

    self.assertEqual(200, resp.status_code)
    self.assert_blogposts([BlogPost(id='X', source=self.source.key,
                                    feed_item={'id': 'X', 'content': 'a ☕ z'},
                                    status='complete')])

  def test_handle_feed_truncates_links(self):
    self.start_patch(superfeedr, 'MAX_BLOGPOST_LINKS', new=2)

    item_a = {'permalinkUrl': 'A',
              'content': 'a http://a http://b http://c z'}
    post_a = BlogPost(id='A', source=self.source.key, feed_item=item_a,
                      unsent=['http://a/', 'http://b/'])

    superfeedr.handle_feed({'items': [item_a]}, self.source)
    self.assert_blogposts([post_a])
    self.assert_task('propagate-blogpost', key=post_a)
