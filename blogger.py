"""Blogger API 2.0 hosted blog implementation.

https://developers.google.com/blogger/docs/2.0/developers_guide_protocol
https://support.google.com/blogger/answer/42064?hl=en
create comment:
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol#CreatingComments

test command line:
curl localhost:8080/webmention/blogger \
  -d 'source=http://localhost/response.html&target=http://freedom-io-2.blogspot.com/2014/04/blog-post.html'
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import json
import logging
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import blogger_v2 as oauth_blogger
from gdata.blogger.client import Query
import models
import util
import webapp2

from google.appengine.ext import ndb


class Blogger(models.Source):
  """A Blogger blog.

  The key name is the blog id.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='Blogger')
  SHORT_NAME = 'blogger'

  def feed_url(self):
    # https://support.google.com/blogger/answer/97933?hl=en
    return urlparse.urljoin(self.domain_url, '/feeds/posts/default')  # Atom

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Blogger for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.blogger.BloggerV2Auth
    """
    url, domain, ok = Blogger._url_and_domain(auth_entity)
    if not ok:
      handler.messages = {'No Blogger blogs found. Please create one first!'}
      return None

    for id, hostname in zip(auth_entity.blog_ids, auth_entity.blog_hostnames):
      if domain == hostname:
        break
    else:
      return self.error("Internal error, shouldn't happen")

    return Blogger(id=id,
                   auth_entity=auth_entity.key,
                   url=url,
                   name=auth_entity.user_display_name(),
                   domain=domain,
                   domain_url=url,
                   picture=auth_entity.picture_url,
                   superfeedr_secret=util.generate_secret(),
                   **kwargs)

  @staticmethod
  def _url_and_domain(auth_entity):
    """Returns an auth entity's URL and domain.

    Args:
      auth_entity: oauth_dropins.blogger.BloggerV2Auth

    Returns: (string url, string domain, boolean ok)
    """
    # TODO: if they have multiple blogs, let them choose which one to sign up.
    domain = next(iter(auth_entity.blog_hostnames), None)
    if not domain:
      return None, None, False
    return 'http://%s/' % domain, domain, True

  def create_comment(self, post_url, author_name, author_url, content, client=None):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string
      client: gdata.blogger.client.BloggerClient. If None, one will be created
        from auth_entity. Used for dependency injection in the unit test.

    Returns: JSON response dict with 'id' and other fields
    """
    if client is None:
      client = self.auth_entity.get().api()

    # extract the post's path and look up its post id
    path = urlparse.urlparse(post_url).path
    logging.info('Looking up post id for %s', path)
    feed = client.get_posts(self.key.id(), query=Query(path=path))

    if not feed.entry:
      return self.error('Could not find Blogger post %s' % post_url)
    elif len(feed.entry) > 1:
      logging.warning('Found %d Blogger posts for path %s , expected 1', path)
    post_id = feed.entry[0].get_post_id()

    # create the comment
    content = '<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    logging.info('Creating comment on blog %s, post %s: %s', self.key.id(),
                 post_id, content)
    resp = client.add_comment(self.key.id(), post_id, content)
    # STATE: need json
    logging.info('Response: %s', resp)
    return resp


class AddBlogger(oauth_blogger.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(Blogger, auth_entity, state)


class OAuthCallback(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Blogger's
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity_str_key = util.get_required_param(self, 'auth_entity')
    state = self.request.get('state')
    if not state:
      # state doesn't currently come through for Blogger. not sure why. doesn't
      # matter for now since we don't plan to implement listen or publish.
      state = 'webmention'
    auth_entity = ndb.Key(urlsafe=auth_entity_str_key).get()
    self.maybe_add_or_delete_source(Blogger, auth_entity, state)


class SuperfeedrNotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """
  def post(self, id):
    source = Blogger.get_by_id()
    if source and 'webmention' in source.features:
      superfeedr.handle_feed(self.request.body, source)


application = webapp2.WSGIApplication([
    ('/blogger/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/oauth2callback', oauth_blogger.CallbackHandler.to('/blogger/add')),
    ('/blogger/add', OAuthCallback),
    ('/blogger/delete/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
