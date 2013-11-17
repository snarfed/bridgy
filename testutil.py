"""Unit test utilities.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import base64
import collections
import datetime
import json
import urlparse

from models import Comment, Source
from tasks import Poll, Propagate
import util
from webutil import testutil

from google.appengine.datastore import datastore_stub_util
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


class FakeSource(FakeBase, Source):
  """Attributes:
    comments: dict mapping FakeSource string key to list of Comments to be
      returned by poll()
  """
  comments = {}

  def set_comments(self, comments):
    FakeSource.comments[str(self.key())] = comments

  def get_posts(self):
    return [(c, c.target_post_url) for c in FakeSource.comments[str(self.key())]]

  def get_comments(self, posts):
    assert posts
    return FakeSource.comments[str(self.key())]


class HandlerTest(testutil.HandlerTest):
  """Base test class.
  """
  def setUp(self):
    super(HandlerTest, self).setUp()
    self.handler = util.Handler(self.request, self.response)
    # TODO: remove this and don't depend on consistent global queries
    self.testbed.init_datastore_v3_stub(consistency_policy=None)


class ModelsTest(HandlerTest):
  """Sets up some test sources and comments.

  Attributes:
    sources: list of FakeSource
    comments: list of unsaved Comment
    taskqueue_stub: the app engine task queue api proxy stub
  """

  def setUp(self):
    super(ModelsTest, self).setUp()

    self.sources = [FakeSource.new(None), FakeSource.new(None)]
    for entity in self.sources:
      entity.save()

    now = datetime.datetime.now()

    mf2_json = json.dumps({
      "type": ["h-entry"],
      "properties": {
        "url": ["http://source/comment/url"],
        "content": [{"value": "foo", "html": "foo"}],
        "in-reply-to": ["http://source/post/url"]
        }
      })

    self.comments = [Comment(key_name=k, source=self.sources[0], mf2_json=mf2_json)
                     for k in ('a', 'b', 'c')]

    self.sources[0].set_comments(self.comments)
