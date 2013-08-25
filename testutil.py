"""Unit test utilities, including a TestCase subclass that sets up testbed.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import base64
import collections
import cStringIO
import datetime
import mox
import re
import urllib
import urlparse
import webob
import wsgiref

from models import Comment, Destination, Source
from tasks import Poll, Propagate
import util

from google.appengine.api import apiproxy_stub_map
from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.ext import testbed
from google.appengine.ext import webapp


def get_task_params(task):
  """Parses a task's POST body and returns the query params in a dict.
  """
  params = urlparse.parse_qs(base64.b64decode(task['body']))
  params = dict((key, val[0]) for key, val in params.items())
  return params


class UrlfetchResult(object):
  """A fake urlfetch.fetch() result object.
  """
  def __init__(self, status_code, content, final_url=None):
    self.status_code = status_code
    self.content = content
    self.final_url = final_url


class TestbedTest(mox.MoxTestBase):
  """Base test case class that sets up App Engine testbed.

  Subclasses must call setup_testbed() before using App Engine APIs!

  For more info on testbed, see:
  http://code.google.com/appengine/docs/python/tools/localunittesting.html
  """

  def setUp(self):
    super(TestbedTest, self).setUp()
    self.testbed = testbed.Testbed()
    self.setup_testbed()
    self.mox.StubOutWithMock(urlfetch, 'fetch')

  def tearDown(self):
    self.testbed.deactivate()
    super(TestbedTest, self).tearDown()

  def setup_testbed(self, **setup_env):
    """Sets up testbed for the current test.

    Args:
      setup_env: keyword arguments to be passed to testbed.setup_env()
    """
    env = dict((key.lower(), value) for key, value in
               testbed.DEFAULT_ENVIRONMENT.items())
    env['federated_identity'] = 'foo.com/bar'
    env.update(setup_env)
    self.testbed.setup_env(overwrite=True, **env)

    self.testbed.activate()
    self.testbed.init_datastore_v3_stub()
    self.testbed.init_taskqueue_stub(root_path='.')
    self.testbed.init_urlfetch_stub()
    self.testbed.init_user_stub()

    # unofficial API, whee! this is so we can call
    # TaskQueueServiceStub.GetTasks() in tests. see
    # google/appengine/api/taskqueue/taskqueue_stub.py
    self.taskqueue_stub = apiproxy_stub_map.apiproxy.GetStub('taskqueue')

  def expect_urlfetch(self, expected_url, response=None, **kwargs):
    """Sets up an expected call to urlfetch.fetch.

    Args:
      expected_url: string, regex or mox.Comparator
      response: string

    Returns: mox.MockMethod that you can call AndReturn(), AndRaise(), etc. on.
    """
    if isinstance(expected_url, mox.Comparator):
      comparator = expected_url
    else:
      comparator = mox.Regex(expected_url)

    if not isinstance(response, UrlfetchResult):
      response = UrlfetchResult(200, response)

    mock = urlfetch.fetch(comparator, deadline=999, **kwargs)
    if response is not None:
      mock.AndReturn(response)
    return mock

  def assert_keys_equal(self, a, b):
    """Asserts that a and b have the same keys.

    Args:
      a, b: db.Model instances or lists of instances
    """
    self.assert_entities_equal(a, b, keys_only=True)

  def assert_entities_equal(self, a, b, ignore=frozenset(), keys_only=False,
                            in_order=False):
    """Asserts that a and b are equivalent entities or lists of entities.

    ...specifically, that they have the same property values, and if they both
    have populated keys, that their keys are equal too.

    Args:
      a, b: db.Model instances or lists of instances
      ignore: sequence of strings, property names not to compare
      keys_only: boolean, if True only compare keys
      in_order: boolean. If False, all entities must have keys.
    """
    if not isinstance(a, (list, tuple, db.Query)):
      a = [a]
    if not isinstance(b, (list, tuple, db.Query)):
      b = [b]

    if not in_order:
      key_fn = lambda e: e.key()
      a = list(sorted(a, key=key_fn))
      b = list(sorted(b, key=key_fn))

    self.assertEqual(len(a), len(b),
                     'Different lengths:\n expected %s\n actual %s' % (a, b))

    for x, y in zip(a, b):
      try:
        self.assertEqual(x.key().to_path(), y.key().to_path())
      except (db.BadKeyError, db.NotSavedError):
        if keys_only:
          raise

      if not keys_only:
        self.assertEqual(x.properties(), y.properties())
        for prop in x.properties().values():
          if prop.name not in ignore:
            x_val = prop.get_value_for_datastore(x)
            y_val = prop.get_value_for_datastore(y)
            self.assertEqual(x_val, y_val,
                             '%s: %r != %r' % (prop.name, x_val, y_val))

  def entity_keys(self, entities):
    """Returns a list of keys for a list of entities.
    """
    return [e.key() for e in entities]


class HandlerTest(TestbedTest):
  """Base test class for HTTP request handlers.
  """

  def setUp(self):
    super(HandlerTest, self).setUp()

    self.environ = {}
    wsgiref.util.setup_testing_defaults(self.environ)
    self.environ['HTTP_HOST'] = 'HOST'
    
    self.gae_user_id = '123'
    self.setup_testbed(user_id=self.gae_user_id,
                       user_email='foo@bar.com',
                       federated_identity='')

    self.request = webapp.Request(self.environ)
    self.response = webapp.Response()
    self.handler = util.Handler()
    self.handler.initialize(self.request, self.response)

  def get(self, *args, **kwargs):
    return self._make_request('GET', *args, **kwargs)
  
  def post(self, *args, **kwargs):
    return self._make_request('POST', *args, **kwargs)

  def _make_request(self, method, application, path, expected_status,
                    query_params=None, post_params=None, headers=None):
    """Makes an internal HTTP request for testing.

    Args:
      method: string, 'GET' or 'POST'
      application: WSGIApplication to test
      path: string, the query URL
      expected_status: integer, expected HTTP response status code
      query_params: dict of string to string, query parameters
      post_params: dict of string to string, POST request parameters
      headers: dict of string: string, the HTTP request headers

    Returns:
      webapp.Response
    """
    assert method
    self.environ['REQUEST_METHOD'] = method

    self.environ['PATH_INFO'] = path
    if query_params:
      self.environ['QUERY_STRING'] = urllib.urlencode(query_params)

    body = ''
    if post_params:
      body = urllib.urlencode(post_params)
    else:
      body = ''
    self.environ['wsgi.input'] = cStringIO.StringIO(body)
    # webob.Request (and hence webapp.Request) only reads CONTENT_LENGTH bytes
    # from wsgi.input, so we have to set it too.
    self.environ['CONTENT_LENGTH'] = len(body)

    if headers:
      webob.Request(self.environ).headers.update(headers)

    def start_response(status, headers, exc_info=None):
      assert exc_info is None
      self.assertTrue(status.startswith(str(expected_status)),
                      'Expected %s but was %s' % (expected_status, status))
      self.response.headers = wsgiref.headers.Headers(headers)
      return self.response.out.write

    application(self.environ, start_response)
    return self.response


class FakeBase(db.Model):
  """Not thread safe.
  """

  key_name_counter = 1

  @classmethod
  def new(cls, handler, **props):
    if 'url' not in props:
      props['url'] = 'http://fake/url'
    inst = cls(key_name=str(cls.key_name_counter), **props)
    cls.key_name_counter += 1
    return inst

  def type_display_name(self):
    return self.__class__.__name__


class FakeSite(FakeBase, Destination):
  pass


class FakeDestination(FakeBase, Destination):
  """  Attributes:
    comments: dict mapping FakeDestination string key to list of Comment entities
  """

  comments = collections.defaultdict(list)

  def add_comment(self, comment):
    FakeDestination.comments[str(self.key())].append(comment)

  def get_comments(self):
    return FakeDestination.comments[str(self.key())]


class FakeSource(FakeBase, Source):
  """Attributes:
    comments: dict mapping FakeSource string key to list of Comments to be
      returned by poll()
  """
  comments = {}

  def set_comments(self, comments):
    FakeSource.comments[str(self.key())] = comments

  def get_posts(self):
    return [(c, c.dest_post_url) for c in FakeSource.comments[str(self.key())]]

  def get_comments(self, posts):
    assert posts
    return FakeSource.comments[str(self.key())]


class ModelsTest(HandlerTest):
  """Sets up some test sources, destinations, and comments.

  Attributes:
    sources: list of FakeSource
    dests: list of FakeDestination
    comments: list of unsaved Comment
    taskqueue_stub: the app engine task queue api proxy stub
  """

  def setUp(self):
    super(ModelsTest, self).setUp()

    self.sources = [FakeSource.new(None), FakeSource.new(None)]
    self.dests = [FakeDestination.new(None, url='http://dest0/'),
                  FakeDestination.new(None, url='http://dest1/'),
                  ]
    for entity in self.sources + self.dests:
      entity.save()

    now = datetime.datetime.now()

    properties = {
      'source': self.sources[0],
      'created': now,
      'source_post_url': 'http://source/post/url',
      'source_comment_url': 'http://source/comment/url',
      'author_name': 'me',
      'author_url': 'http://me',
      'content': 'foo',
      }

    self.comments = [
      Comment(key_name='a',
              dest=self.dests[1],
              dest_post_url='http://dest1/post/url',
              dest_comment_url='http://dest1/comment/a/url',
              **properties),
      Comment(key_name='b',
              dest=self.dests[0],
              dest_post_url='http://dest0/post/url',
              dest_comment_url='http://dest0/comment/b/url',
              **properties),
      Comment(key_name='c',
              dest=self.dests[1],
              dest_post_url='http://dest1/post/url',
              dest_comment_url='http://dest1/comment/c/url',
              **properties),
      ]

    self.sources[0].set_comments(self.comments)
