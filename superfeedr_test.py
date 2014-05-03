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
    superfeedr.SOURCES['fake'] = testutil.FakeSource
    self.source = testutil.FakeSource(id='foo.com', domain='foo.com',
                                      features=['webmention'])
    self.source.put()

  def test_subscribe(self):
    expected = {
      'hub.mode': 'subscribe',
      'hub.topic': 'fake feed url',
      'hub.callback': 'http://localhost/superfeedr/notify/fake/foo.com',
      'hub.secret': 'xxx',
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

  def test_notify(self):
    item_a = {'permalinkUrl': 'A', 'content': 'a http://a.com a'}
    resp = superfeedr.application.get_response(
      '/superfeedr/notify/fake/foo.com', method='POST',
      body=json.dumps({'items': [item_a]}),
      content_type='application/json')
    self.assertEqual(200, resp.status_int)

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
    superfeedr.handle_feed({}, self.source)
    self.assertEquals(0, BlogPost.query().count())
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate-blogpost')))
