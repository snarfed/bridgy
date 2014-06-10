"""Blogger API 2.0 hosted blog implementation.

To use, go to your Blogger blog's dashboard, click Template, Edit HTML, then
put this in the head section:

<link rel="webmention" href="https://www.brid.gy/webmention/blogger"></link>

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
from gdata.client import RequestError
import models
import superfeedr
import util
import webapp2

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template


class Blogger(models.Source):
  """A Blogger blog.

  The key name is the blog id.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='Blogger')
  SHORT_NAME = 'blogger'

  def feed_url(self):
    # https://support.google.com/blogger/answer/97933?hl=en
    return urlparse.urljoin(self.domain_url, '/feeds/posts/default')  # Atom

  def silo_url(self):
    return self.domain_url

  def edit_template_url(self):
    return 'https://www.blogger.com/blogger.g?blogID=%s#template' % self.key.id()

  @staticmethod
  def new(handler, auth_entity=None, blog_id=None, **kwargs):
    """Creates and returns a Blogger for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.blogger.BloggerV2Auth
      blog_id: which blog. optional. if not provided, uses the first available.
    """
    url, domain, ok = Blogger._url_and_domain(auth_entity, blog_id=blog_id)
    if not ok:
      handler.messages = {'Blogger blog not found. Please create one first!'}
      return None

    return Blogger(id=blog_id,
                   auth_entity=auth_entity.key,
                   url=url,
                   name=auth_entity.user_display_name(),
                   domain=domain,
                   domain_url=url,
                   picture=auth_entity.picture_url,
                   superfeedr_secret=util.generate_secret(),
                   **kwargs)

  @staticmethod
  def _url_and_domain(auth_entity, blog_id=None):
    """Returns an auth entity's URL and domain.

    Args:
      auth_entity: oauth_dropins.blogger.BloggerV2Auth
      blog_id: which blog. optional. if not provided, uses the first available.

    Returns: (string url, string domain, boolean ok)
    """
    for id, host in zip(auth_entity.blog_ids, auth_entity.blog_hostnames):
      if blog_id == id or (not blog_id and host):
        return 'http://%s/' % host, host, True

    return None, None, False

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
    content = u'<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    logging.info('Creating comment on blog %s, post %s: %s', self.key.id(),
                 post_id, content.encode('utf-8'))
    try:
      comment = client.add_comment(self.key.id(), post_id, content)
    except RequestError, e:
      msg = str(e)
      if 'bX-2i87au' in msg or 'bX-at8zpb' in msg:
        # known error: https://github.com/snarfed/bridgy/issues/175
        return {'error': msg}

    resp = {'id': comment.get_comment_id(), 'response': comment.to_string()}
    logging.info('Response: %s', resp)
    return resp


class OAuthHandler(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Blogger's
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity_str_key = self.request.get('auth_entity')
    if auth_entity_str_key:
      auth_entity = ndb.Key(urlsafe=auth_entity_str_key).get()
    else:
      self.messages.add(
        "Couldn't fetch your blogs. Maybe you're not a Blogger user?")
      return

    state = self.request.get('state')
    if not state:
      # state doesn't currently come through for Blogger. not sure why. doesn't
      # matter for now since we don't plan to implement listen or publish.
      state = 'webmention'

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
    self.response.out.write(template.render('templates/choose_blog.html', vars))


class AddBlogger(util.Handler):
  def post(self):
    auth_entity_key = util.get_required_param(self, 'auth_entity_key')
    self.maybe_add_or_delete_source(
      Blogger,
      ndb.Key(urlsafe=auth_entity_key).get(),
      util.get_required_param(self, 'state'),
      blog_id=util.get_required_param(self, 'blog'),
      )


class SuperfeedrNotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """
  def post(self, id):
    source = Blogger.get_by_id(id)
    if source and 'webmention' in source.features:
      superfeedr.handle_feed(self.request.body, source)


application = webapp2.WSGIApplication([
    ('/blogger/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/delete/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/oauth2callback', oauth_blogger.CallbackHandler.to('/blogger/oauth_handler')),
    ('/blogger/oauth_handler', OAuthHandler),
    ('/blogger/add', AddBlogger),
    ('/blogger/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
