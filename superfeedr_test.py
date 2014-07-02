"""Unit tests for superfeedr.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox

from appengine_config import HTTP_TIMEOUT
from models import BlogPost

import superfeedr
import testutil


class SuperfeedrTest(testutil.HandlerTest):

  def setUp(self):
    super(SuperfeedrTest, self).setUp()
    self.source = testutil.FakeSource(id='foo.com', domains=['foo.com'],
                                      features=['webmention'])
    self.source.put()

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
    item_a = {'permalinkUrl': 'A', 'content': 'a http://a.com a'}
    superfeedr.handle_feed(json.dumps({'items': [item_a]}), self.source)

    posts = list(BlogPost.query())
    self.assert_entities_equal(
      [BlogPost(id='A', source=self.source.key, feed_item=item_a,
                unsent=['http://a.com'])],
      posts,
      ignore=('created', 'updated'))

    tasks = self.taskqueue_stub.GetTasks('propagate-blogpost')
    self.assertEqual(1, len(tasks))
    self.assert_equals(posts[0].key.urlsafe(),
                       testutil.get_task_params(tasks[0])['key'])

  def test_handle_feed_no_items(self):
    superfeedr.handle_feed('{}', self.source)
    self.assertEquals(0, BlogPost.query().count())
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate-blogpost')))

  def test_preprocess_superfeedr_item(self):
    self.mox.StubOutWithMock(self.source, 'preprocess_superfeedr_item')
    items = [{'permalinkUrl': 'A', 'content': 'a b'}]

    def add_link(item):
      item['content'] += '\nhttp://added/by/preprocess'
    self.source.preprocess_superfeedr_item(items[0]).WithSideEffects(add_link)

    self.mox.ReplayAll()
    superfeedr.handle_feed(json.dumps({'items': items}), self.source)
    self.assertEquals(['http://added/by/preprocess'], BlogPost.query().get().unsent)
