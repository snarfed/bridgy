"""Unit tests for superfeedr.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json

import mox

from models import BlogPost
import superfeedr
import testutil
import webapp2


class SuperfeedrTest(testutil.HandlerTest):

  def setUp(self):
    super(SuperfeedrTest, self).setUp()
    self.source = testutil.FakeSource(id='foo.com', domains=['foo.com'],
                                      features=['webmention'])
    self.source.put()
    self.item = {'id': 'A', 'content': 'B'}
    self.feed = json.dumps({'items': [self.item]})

  def assert_blogposts(self, count):
    self.assertEquals(count, BlogPost.query().count())
    self.assertEquals(count, len(self.taskqueue_stub.GetTasks('propagate-blogpost')))

  def test_subscribe(self):
    expected = {
      'hub.mode': 'subscribe',
      'hub.topic': 'fake feed url',
      'hub.callback': 'http://localhost/fake/notify/foo.com',
      'format': 'json',
      'retrieve': 'true',
      }
    item_a = {'permalinkUrl': 'A', 'content': 'a http://a.com a'}
    item_b = {'permalinkUrl': 'B', 'summary': 'b http://b.com b'}
    feed = json.dumps({'items': [item_a, {}, item_b]})
    self.expect_requests_post(superfeedr.PUSH_API_URL, feed,
                              data=expected, auth=mox.IgnoreArg())
    self.mox.ReplayAll()

    superfeedr.subscribe(self.source, self.handler)

    posts = list(BlogPost.query())
    self.assert_entities_equal(
      [BlogPost(id='A', source=self.source.key, feed_item=item_a,
                unsent=['http://a.com']),
       BlogPost(id='B', source=self.source.key, feed_item=item_b,
                unsent=['http://b.com']),
       ], posts,
      ignore=('created', 'updated'))

    tasks = self.taskqueue_stub.GetTasks('propagate-blogpost')
    self.assert_equals([{'key': posts[0].key.urlsafe()},
                        {'key': posts[1].key.urlsafe()}],
                       [testutil.get_task_params(t) for t in tasks])

  def test_handle_feed(self):
    item_a = {'permalinkUrl': 'A',
              'content': 'a http://a.com http://foo.com/self/link b'}
    superfeedr.handle_feed(json.dumps({'items': [item_a]}), self.source)

    posts = list(BlogPost.query())
    self.assert_entities_equal(
      [BlogPost(id='A', source=self.source.key, feed_item=item_a,
                unsent=['http://a.com'])],  # self link should be discarded
      posts,
      ignore=('created', 'updated'))

    tasks = self.taskqueue_stub.GetTasks('propagate-blogpost')
    self.assertEqual(1, len(tasks))
    self.assert_equals(posts[0].key.urlsafe(),
                       testutil.get_task_params(tasks[0])['key'])

  def test_handle_feed_no_items(self):
    superfeedr.handle_feed('{}', self.source)
    self.assert_blogposts(0)

  def test_handle_feed_disabled_source(self):
    self.source.status = 'disabled'
    self.source.put()
    superfeedr.handle_feed(self.feed, self.source)
    self.assert_blogposts(0)

  def test_handle_feed_source_missing_webmention_feature(self):
    self.source.features = ['listen']
    self.source.put()
    superfeedr.handle_feed(self.feed, self.source)
    self.assert_blogposts(0)

  def test_handle_feed_allows_bridgy_publish_links(self):
    item = {'permalinkUrl': 'A',
            'content': 'a https://brid.gy/publish/facebook b'}
    superfeedr.handle_feed(json.dumps({'items': [item]}), self.source)
    self.assert_equals(['https://brid.gy/publish/facebook'],
                       BlogPost.get_by_id('A').unsent)

  def test_notify_handler(self):
    class Handler(superfeedr.NotifyHandler):
      SOURCE_CLS = testutil.FakeSource

    app = webapp2.WSGIApplication([('/notify/(.+)', Handler)], debug=True)
    self.feed = json.dumps({'items': [{'id': 'X', 'content': 'a http://x/y z'}]})
    resp = app.get_response('/notify/foo.com', method='POST', body=self.feed)

    self.assertEquals(200, resp.status_int)
    self.assert_blogposts(1)
