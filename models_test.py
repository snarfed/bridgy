# coding=utf-8
"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import urllib

from models import BlogPost, Response, Source, SyndicatedPost
import mox
import superfeedr
import testutil
from testutil import FakeSource
import util

from activitystreams import source as as_source
from google.appengine.api import users
from google.appengine.ext import testbed


class ResponseTest(testutil.ModelsTest):

  def test_get_or_save(self):
    self.sources[0].put()

    response = self.responses[0]
    self.assertEqual(0, Response.query().count())
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

    # new. should add a propagate task.
    saved = response.get_or_save()
    self.assertEqual(response.key, saved.key)
    self.assertEqual(response.source, saved.source)
    self.assertEqual('comment', saved.type)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEqual(1, len(tasks))
    self.assertEqual(response.key.urlsafe(),
                     testutil.get_task_params(tasks[0])['response_key'])
    self.assertEqual('/_ah/queue/propagate', tasks[0]['url'])

    # existing. no new task.
    same = saved.get_or_save()
    self.assertEqual(saved.source, same.source)
    self.assertEqual(1, len(tasks))

  def test_get_or_save_objectType_note(self):
    self.responses[0].response_json = json.dumps({
      'objectType': 'note',
      'id': 'tag:source.com,2013:1_2_%s' % id,
      })
    saved = self.responses[0].get_or_save()
    self.assertEqual('comment', saved.type)

  def test_url(self):
    self.assertEqual('http://localhost/fake/%s' % self.sources[0].key.string_id(),
                     self.sources[0].bridgy_url(self.handler))

  def test_get_or_save_empty_unsent_no_task(self):
    self.responses[0].unsent = []
    saved = self.responses[0].get_or_save()
    self.assertEqual('complete', saved.status)
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

  def test_get_type(self):
    self.assertEqual('repost', Response.get_type(
        {'objectType': 'activity', 'verb': 'share'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'rsvp-no'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'invite'}))
    self.assertEqual('comment', Response.get_type({'objectType': 'other'}))

  def test_hooks(self):
    resp = Response(id='x', activity_json='{"foo": "bar"}')
    self.assertRaises(AssertionError, resp.put)

    pre_put = Response._pre_put_hook
    del Response._pre_put_hook
    resp.put()
    Response._pre_put_hook = pre_put
    got = resp.key.get()
    self.assertEqual(['{"foo": "bar"}'], got.activities_json)
    self.assertIsNone(got.activity_json)


class SourceTest(testutil.HandlerTest):

  def _test_create_new(self, **kwargs):
    FakeSource.create_new(self.handler, domains=['foo'],
                          domain_urls=['http://foo.com'],
                          webmention_endpoint='http://x/y',
                          **kwargs)
    self.assertEqual(1, FakeSource.query().count())

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    source = FakeSource.query().get()
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    self.assertEqual(source.key.urlsafe(),
                     testutil.get_task_params(tasks[0])['source_key'])

  def test_create_new(self):
    self.assertEqual(0, FakeSource.query().count())
    self._test_create_new(features=['listen'])
    msg = "Added fake (FakeSource). Refresh to see what we've found!"
    self.assert_equals({msg}, self.handler.messages)

    task_params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll')[0])
    self.assertEqual('1970-01-01-00-00-00', task_params['last_polled'])

  def test_create_new_already_exists(self):
    long_ago = datetime.datetime(year=1901, month=2, day=3)
    props = {
      'created': long_ago,
      'last_webmention_sent': long_ago + datetime.timedelta(days=1),
      'last_polled': long_ago + datetime.timedelta(days=2),
      'last_hfeed_fetch': long_ago + datetime.timedelta(days=3),
      'last_syndication_url': long_ago + datetime.timedelta(days=4),
      'superfeedr_secret': 'asdfqwert',
      }
    FakeSource.new(None, features=['listen'], **props).put()
    self.assert_equals(['listen'], FakeSource.query().get().features)

    FakeSource.string_id_counter -= 1
    auth_entity = testutil.FakeAuthEntity(
      id='x', user_json=json.dumps({'url': 'http://foo.com/'}))
    auth_entity.put()
    self._test_create_new(auth_entity=auth_entity, features=['publish'])

    source = FakeSource.query().get()
    self.assert_equals(['listen', 'publish'], source.features)
    for prop, value in props.items():
      self.assert_equals(value, getattr(source, prop), prop)

    self.assert_equals(
      {"Updated fake (FakeSource). Try previewing a post from your web site!"},
      self.handler.messages)

    task_params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll')[0])
    self.assertEqual('1901-02-05-00-00-00', task_params['last_polled'])

  def test_create_new_publish(self):
    """If a source is publish only, we shouldn't insert a poll task."""
    FakeSource.create_new(self.handler, features=['publish'])
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll')))

  def test_create_new_webmention(self):
    """We should subscribe to webmention sources in Superfeedr."""
    self.expect_requests_get('http://primary/', 'no webmention endpoint',
                             verify=False)
    self.mox.StubOutWithMock(superfeedr, 'subscribe')
    superfeedr.subscribe(mox.IsA(FakeSource), self.handler)

    self.mox.ReplayAll()
    source = FakeSource.create_new(self.handler, features=['webmention'],
                                   domains=['primary/'],
                                   domain_urls=['http://primary/'])

  def test_create_new_domain(self):
    """If the source has a URL set, extract its domain."""
    # bad URLs
    for user_json in (None, {}, {'url': 'not<a>url'},
                      # t.co is in the webmention blacklist
                      {'url': 'http://t.co/foo'}):
      auth_entity = None
      if user_json is not None:
        auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(user_json))
        auth_entity.put()
      source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
      self.assertEqual([], source.domains)
      self.assertEqual([], source.domain_urls)

    # good URLs
    for url in ('http://foo.com/bar', 'https://www.foo.com/bar',
                'http://FoO.cOm',  # should be normalized to lowercase
                ):
      auth_entity = testutil.FakeAuthEntity(
        id='x', user_json=json.dumps({'url': url}))
      auth_entity.put()
      source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
      self.assertEquals([url], source.domain_urls)
      self.assertEquals(['foo.com'], source.domains)

    # multiple good URLs, a bad URL, and a good URL that returns fails a HEAD
    # request.
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps({
          'url': 'http://foo.org',
          'urls': [{'value': u} for u in
                   'http://bar.com', 'http://t.co/x', 'http://baz'],
          }))
    auth_entity.put()
    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['http://foo.org', 'http://bar.com', 'http://baz'],
                      source.domain_urls)
    self.assertEquals(['foo.org', 'bar.com', 'baz'], source.domains)

  def test_create_new_unicode_chars(self):
    """We should handle unusual unicode chars in the source's name ok."""
    # the invisible character in the middle is an unusual unicode character
    source = FakeSource.create_new(self.handler, name=u'a ✁ b')

  def test_create_new_rereads_domains(self):
    FakeSource.new(None, features=['listen'],
                   domain_urls=['http://foo'], domains=['foo']).put()

    FakeSource.string_id_counter -= 1
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(
        {'urls': [{'value': 'http://bar'}, {'value': 'http://baz'}]}))
    self.expect_requests_get('http://bar', 'no webmention endpoint',
                             verify=False)

    self.mox.ReplayAll()
    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['http://bar', 'http://baz'], source.domain_urls)
    self.assertEquals(['bar', 'baz'], source.domains)

  def test_verify(self):
    # this requests.get is called by webmention-tools
    self.expect_requests_get('http://primary/', """
<html><meta>
<link rel="webmention" href="http://web.ment/ion">
</meta></html>""", verify=False)
    self.mox.ReplayAll()

    source = FakeSource.new(self.handler, features=['webmention'],
                            domain_urls=['http://primary/'], domains=['primary'])
    source.verify()
    self.assertEquals('http://web.ment/ion', source.webmention_endpoint)

  def test_verify_without_webmention_endpoint(self):
    self.expect_requests_get('http://primary/', 'no webmention endpoint here!',
                             verify=False)
    self.mox.ReplayAll()

    source = FakeSource.new(self.handler, features=['webmention'],
                            domain_urls=['http://primary/'], domains=['primary'])
    source.verify()
    self.assertIsNone(source.webmention_endpoint)

  def test_get_post(self):
    post = {'verb': 'post', 'object': {'objectType': 'note', 'content': 'asdf'}}
    source = Source(id='x')
    self.mox.StubOutWithMock(source, 'get_activities')
    source.get_activities(activity_id='123', user_id='x').AndReturn([post])

    self.mox.ReplayAll()
    self.assert_equals(post, source.get_post('123'))

  def test_get_comment(self):
    comment_obj = {'objectType': 'comment', 'content': 'qwert'}
    source = FakeSource.new(None)
    source.as_source = self.mox.CreateMock(as_source.Source)
    source.as_source.get_comment('123', activity_id=None, activity_author_id=None
                                 ).AndReturn(comment_obj)

    self.mox.ReplayAll()
    self.assert_equals(comment_obj, source.get_comment('123'))


class BlogPostTest(testutil.ModelsTest):

  def test_label(self):
    for feed_item in None, {}:
      bp = BlogPost(id='x')
      bp.put()
      self.assertEquals('BlogPost x [no url]', bp.label())

    bp = BlogPost(id='x', feed_item={'permalinkUrl': 'http://perma/link'})
    bp.put()
    self.assertEquals('BlogPost x http://perma/link', bp.label())


class SyndicatedPostTest(testutil.ModelsTest):

  def setUp(self):
    super(SyndicatedPostTest, self).setUp()

    self.source = FakeSource.new(None)
    self.source.put()

    self.relationships = []
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/post/url',
                       syndication='http://silo/post/url'))
    # two syndication for the same original
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/post/url',
                       syndication='http://silo/another/url'))
    # two originals for the same syndication
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/another/post',
                       syndication='http://silo/post/url'))
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original=None,
                       syndication='http://silo/no-original'))
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/no-syndication',
                       syndication=None))

    for r in self.relationships:
      r.put()

  def test_insert_replaces_blanks(self):
    """Make sure we replace original=None with original=something
    when it is discovered"""

    # add a blank for the original too
    SyndicatedPost.insert_original_blank(
      self.source, 'http://original/newly-discovered')

    self.assertTrue(
      SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        SyndicatedPost.original == None, ancestor=self.source.key).get())

    self.assertTrue(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://original/newly-discovered',
        SyndicatedPost.syndication == None, ancestor=self.source.key).get())

    r = SyndicatedPost.insert(
        self.source, 'http://silo/no-original',
        'http://original/newly-discovered')
    self.assertIsNotNone(r)
    self.assertEquals('http://original/newly-discovered', r.original)

    # make sure it's in NDB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        ancestor=self.source.key
    ).fetch()
    self.assertEquals(1, len(rs))
    self.assertEquals('http://original/newly-discovered', rs[0].original)
    self.assertEquals('http://silo/no-original', rs[0].syndication)

    # and the blanks have been removed
    self.assertFalse(
      SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        SyndicatedPost.original == None, ancestor=self.source.key).get())

    self.assertFalse(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://original/newly-discovered',
        SyndicatedPost.syndication == None, ancestor=self.source.key).get())

  def test_insert_auguments_existing(self):
    """Make sure we add newly discovered urls for a given syndication url,
    rather than overwrite them
    """
    r = SyndicatedPost.insert(
        self.source, 'http://silo/post/url',
        'http://original/different/url')
    self.assertIsNotNone(r)
    self.assertEquals('http://original/different/url', r.original)

    # make sure they're both in the DB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/post/url',
        ancestor=self.source.key
    ).fetch()

    self.assertItemsEqual(['http://original/post/url',
                           'http://original/another/post',
                           'http://original/different/url'],
                          [rel.original for rel in rs])

  def test_get_or_insert_by_syndication_do_not_duplicate_blanks(self):
    """Make sure we don't insert duplicate blank entries"""

    SyndicatedPost.insert_syndication_blank(
      self.source, 'http://silo/no-original')

    # make sure there's only one in the DB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        ancestor=self.source.key
    ).fetch()

    self.assertItemsEqual([None], [rel.original for rel in rs])

  def test_insert_no_duplicates(self):
    """Make sure we don't insert duplicate entries"""

    r = SyndicatedPost.insert(
      self.source, 'http://silo/post/url', 'http://original/post/url')
    self.assertIsNotNone(r)
    self.assertEqual('http://original/post/url', r.original)

    # make sure there's only one in the DB
    rs = SyndicatedPost.query(
      SyndicatedPost.syndication == 'http://silo/post/url',
      SyndicatedPost.original == 'http://original/post/url',
      ancestor=self.source.key
    ).fetch()

    self.assertEqual(1, len(rs))
