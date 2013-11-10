"""Unit test utilities.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import base64
import collections
import datetime
import urlparse

from models import Comment, Destination, Source
from tasks import Poll, Propagate
from webutil.testutil import *

from google.appengine.ext import db


def get_task_params(task):
  """Parses a task's POST body and returns the query params in a dict.
  """
  params = urlparse.parse_qs(base64.b64decode(task['body']))
  params = dict((key, val[0]) for key, val in params.items())
  return params


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
    comments: dict mapping FakeDestination string key to list of Comments
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
