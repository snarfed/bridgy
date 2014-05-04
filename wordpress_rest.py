"""WordPress REST API (including WordPress.com) hosted blog implementation.

https://developer.wordpress.com/docs/api/
create returns id, can lookup by id

test command line:
curl localhost:8080/webmention/wordpress \
  -d 'source=http://localhost/response.html&target=http://ryandc.wordpress.com/2013/03/24/mac-os-x/'
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import json
import logging
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import wordpress_rest as oauth_wordpress
import models
import util
import webapp2

API_CREATE_COMMENT_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/%d/replies/new?pretty=true'
API_POST_SLUG_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/slug:%s?pretty=true'


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the blog hostname.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='WordPress.com')
  SHORT_NAME = 'wordpress'

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.wordpress.WordPressAuth
    """
    avatar = (json.loads(auth_entity.user_json).get('avatar_URL')
              if auth_entity.user_json else None)
    wp = WordPress(id=auth_entity.key.id(),
                   auth_entity=auth_entity.key,
                   name=auth_entity.user_display_name(),
                   picture=avatar,
                   superfeedr_secret=util.generate_secret(),
                   **kwargs)

    url, domain, _ = wp._url_and_domain(auth_entity)
    wp.url = url
    wp.domain_url = url
    wp.domain = domain

    return wp

  def feed_url(self):
    # http://en.support.wordpress.com/feeds/
    return urlparse.urljoin(self.domain_url, '/feed/')

  def _url_and_domain(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: oauth_dropins.wordpress_rest.WordPressAuth

    Returns: (string url, string domain, True)
    """
    return auth_entity.blog_url, auth_entity.key.id(), True


  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: JSON response dict with 'id' and other fields
    """
    auth_entity = self.auth_entity.get()

    # extract the post's slug and look up its post id
    logging.info('Looking up WordPress.com post id for %s', post_url)
    path = urlparse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      url = API_POST_SLUG_URL % (auth_entity.key.id(), slug)
      resp = auth_entity.urlopen(url).read()

    post_id = json.loads(resp).get('ID')
    if not post_id:
      return self.error('Could not find WordPress.com post for slug %s' % slug)

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.key.id(), post_id)
    data = {'content': '<a href="%s">%s</a>: %s' % (author_url, author_name, content)}
    resp = auth_entity.urlopen(url, data=urllib.urlencode(data)).read()
    resp = json.loads(resp)
    resp['id'] = resp.pop('ID', None)
    return resp


class AddWordPress(oauth_wordpress.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(WordPress, auth_entity, state)


class SuperfeedrNotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """
  def post(self, id):
    source = WordPress.get_by_id()
    if source and 'webmention' in source.features:
      superfeedr.handle_feed(self.request.body, source)


application = webapp2.WSGIApplication([
    ('/wordpress/start', oauth_wordpress.StartHandler.to('/wordpress/add')),
    ('/wordpress/add', AddWordPress),
    ('/wordpress/delete/start', oauth_wordpress.CallbackHandler.to('/delete/finish')),
    ('/wordpress/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
