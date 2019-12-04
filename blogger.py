"""Blogger API 2.0 hosted blog implementation.

Blogger API docs:
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol

Python GData API docs:
http://gdata-python-client.googlecode.com/hg/pydocs/gdata.blogger.data.html

To use, go to your Blogger blog's dashboard, click Template, Edit HTML, then
put this in the head section:

<link rel="webmention" href="https://brid.gy/webmention/blogger"></link>

https://developers.google.com/blogger/docs/2.0/developers_guide_protocol
https://support.google.com/blogger/answer/42064?hl=en
create comment:
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol#CreatingComments

test command line:
curl localhost:8080/webmention/blogger \
  -d 'source=http://localhost/response.html&target=http://freedom-io-2.blogspot.com/2014/04/blog-post.html'
"""
from __future__ import unicode_literals

from future import standard_library
standard_library.install_aliases()
from builtins import str
from builtins import zip
import collections
import logging
import re
import urllib.parse

import appengine_config

from gdata.blogger.client import Query
from gdata.client import Error
from google.cloud import ndb
from oauth_dropins import blogger as oauth_blogger
from oauth_dropins.webutil.handlers import JINJA_ENV

import models
import superfeedr
import util
import webapp2

# Blogger says it's 4096 in an error message. (Couldn't find it in their docs.)
# We include some padding.
# Background: https://github.com/snarfed/bridgy/issues/242
MAX_COMMENT_LENGTH = 4000


class Blogger(models.Source):
  """A Blogger blog.

  The key name is the blog id.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='Blogger')
  OAUTH_START_HANDLER = oauth_blogger.StartHandler
  SHORT_NAME = 'blogger'
  PATH_BLACKLIST = (re.compile('^/search/.*'),)

  def feed_url(self):
    # https://support.google.com/blogger/answer/97933?hl=en
    return urllib.parse.urljoin(self.url, '/feeds/posts/default')  # Atom

  def silo_url(self):
    return self.url

  def edit_template_url(self):
    return 'https://www.blogger.com/blogger.g?blogID=%s#template' % self.key.id()

  @staticmethod
  def new(handler, auth_entity=None, blog_id=None, **kwargs):
    """Creates and returns a Blogger for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.blogger.BloggerUser`
      blog_id: which blog. optional. if not provided, uses the first available.
    """
    urls, domains = Blogger._urls_and_domains(auth_entity, blog_id=blog_id)
    if not urls or not domains:
      handler.messages = {'Blogger blog not found. Please create one first!'}
      return None

    if blog_id is None:
      for blog_id, hostname in zip(auth_entity.blog_ids, auth_entity.blog_hostnames):
        if domains[0] == hostname:
          break
      else:
        assert False, "Internal error, shouldn't happen"

    return Blogger(id=blog_id,
                   auth_entity=auth_entity.key,
                   url=urls[0],
                   name=auth_entity.user_display_name(),
                   domains=domains,
                   domain_urls=urls,
                   picture=auth_entity.picture_url,
                   superfeedr_secret=util.generate_secret(),
                   **kwargs)

  @staticmethod
  def _urls_and_domains(auth_entity, blog_id=None):
    """Returns an auth entity's URL and domain.

    Args:
      auth_entity: oauth_dropins.blogger.BloggerV2Auth
      blog_id: which blog. optional. if not provided, uses the first available.

    Returns:
      ([string url], [string domain])
    """
    for id, host in zip(auth_entity.blog_ids, auth_entity.blog_hostnames):
      if blog_id == id or (not blog_id and host):
        return ['http://%s/' % host], [host]

    return [], []

  def create_comment(self, post_url, author_name, author_url, content, client=None):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string
      client: :class:`gdata.blogger.client.BloggerClient`. If None, one will be
        created from auth_entity. Used for dependency injection in the unit
        test.

    Returns:
      JSON response dict with 'id' and other fields
    """
    if client is None:
      client = self.auth_entity.get().api()

    # extract the post's path and look up its post id
    path = urllib.parse.urlparse(post_url).path
    logging.info('Looking up post id for %s', path)
    feed = client.get_posts(self.key.id(), query=Query(path=path))

    if not feed.entry:
      return self.error('Could not find Blogger post %s' % post_url)
    elif len(feed.entry) > 1:
      logging.warning('Found %d Blogger posts for path %s , expected 1',
                      len(feed.entry), path)
    post_id = feed.entry[0].get_post_id()

    # create the comment
    content = '<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    if len(content) > MAX_COMMENT_LENGTH:
      content = content[:MAX_COMMENT_LENGTH - 3] + '...'
    logging.info('Creating comment on blog %s, post %s: %s', self.key.id(),
                 post_id, content.encode('utf-8'))
    try:
      comment = client.add_comment(self.key.id(), post_id, content)
    except Error as e:
      msg = str(e)
      if ('Internal error:' in msg):
        # known errors. e.g. https://github.com/snarfed/bridgy/issues/175
        # https://groups.google.com/d/topic/bloggerdev/szGkT5xA9CE/discussion
        return {'error': msg}
      else:
        raise

    resp = {'id': comment.get_comment_id(), 'response': comment.to_string()}
    logging.info('Response: %s', resp)
    return resp


class OAuthCallback(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Blogger's
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity = None
    auth_entity_str_key = self.request.get('auth_entity')
    if auth_entity_str_key:
      auth_entity = ndb.Key(urlsafe=auth_entity_str_key).get()
      if not auth_entity.blog_ids or not auth_entity.blog_hostnames:
        auth_entity = None

    if not auth_entity:
      self.messages.add(
        "Couldn't fetch your blogs. Maybe you're not a Blogger user?")

    state = self.request.get('state')
    if not state:
      # state doesn't currently come through for Blogger. not sure why. doesn't
      # matter for now since we don't plan to implement listen or publish.
      state = self.construct_state_param_for_add(feature='webmention')

    if not auth_entity:
      self.maybe_add_or_delete_source(Blogger, auth_entity, state)
      return

    vars = {
      'action': '/blogger/add',
      'state': state,
      'auth_entity_key': auth_entity.key.urlsafe(),
      'blogs': [{'id': id, 'title': title, 'domain': host}
                for id, title, host in zip(auth_entity.blog_ids,
                                           auth_entity.blog_titles,
                                           auth_entity.blog_hostnames)],
      }
    logging.info('Rendering choose_blog.html with %s', vars)

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(JINJA_ENV.get_template('choose_blog.html').render(**vars))


class AddBlogger(util.Handler):
  def post(self):
    auth_entity_key = util.get_required_param(self, 'auth_entity_key')
    self.maybe_add_or_delete_source(
      Blogger,
      ndb.Key(urlsafe=auth_entity_key).get(),
      util.get_required_param(self, 'state'),
      blog_id=util.get_required_param(self, 'blog'),
      )


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = Blogger


application = webapp2.WSGIApplication([
    # Blogger only has one OAuth scope. oauth-dropins fills it in.
    # https://developers.google.com/blogger/docs/2.0/developers_guide_protocol#OAuth2Authorizing
    ('/blogger/start', util.oauth_starter(oauth_blogger.StartHandler).to(
      '/blogger/oauth2callback')),
    ('/blogger/oauth2callback', oauth_blogger.CallbackHandler.to('/blogger/oauth_handler')),
    ('/blogger/oauth_handler', OAuthCallback),
    ('/blogger/add', AddBlogger),
    ('/blogger/delete/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
