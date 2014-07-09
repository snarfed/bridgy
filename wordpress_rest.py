"""WordPress REST API (including WordPress.com) hosted blog implementation.

To use, go to your WordPress.com blog's admin console, then go to Appearance,
Widgets, add a Text widget, and put this in its text section:

<a href="https://www.brid.gy/webmention/wordpress" rel="webmention"></a>

(not this, it breaks :/)
<link rel="webmention" href="https://www.brid.gy/webmention/wordpress">

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
import re
import urllib
import urllib2
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import wordpress_rest as oauth_wordpress
import models
import superfeedr
import util
import webapp2

from google.appengine.ext import ndb


API_CREATE_COMMENT_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/%d/replies/new?pretty=true'
API_POST_SLUG_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/slug:%s?pretty=true'
API_SITE_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s?pretty=true'


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the blog hostname.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='WordPress.com')
  SHORT_NAME = 'wordpress'

  site_info = ndb.JsonProperty(compressed=True)  # from /sites/$site API call

  def feed_url(self):
    # http://en.support.wordpress.com/feeds/
    return urlparse.urljoin(self.domain_urls[0], 'feed/')

  def silo_url(self):
    return self.domain_urls[0]

  def edit_template_url(self):
    return urlparse.urljoin(self.domain_urls[0], 'wp-admin/widgets.php')

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.wordpress.WordPressAuth
    """
    # Fetch blog's site info
    auth_domain = auth_entity.key.id()
    site_info = json.loads(auth_entity.urlopen(
        API_SITE_URL % auth_entity.blog_id).read())
    site_url = site_info.get('URL')
    if site_url:
      domains = [util.domain_from_link(site_url), auth_domain]
      urls = [site_url, auth_entity.blog_url]
    else:
      domains = [auth_domain]
      urls = [auth_entity.blog_url]

    avatar = (json.loads(auth_entity.user_json).get('avatar_URL')
              if auth_entity.user_json else None)
    return WordPress(id=domains[0],
                     auth_entity=auth_entity.key,
                     name=auth_entity.user_display_name(),
                     picture=avatar,
                     superfeedr_secret=util.generate_secret(),
                     url=urls[0],
                     domain_urls=urls,
                     domains=domains,
                     site_info=site_info,
                     **kwargs)

  def _url_and_domain(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: oauth_dropins.wordpress_rest.WordPressAuth, unused

    Returns: (string url, string domain, True)
    """
    return self.url, self.key.id()


  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    If the last part of the post URL is numeric, e.g. http://site/post/123999,
    it's used as the post id. Otherwise, we extract the last part of
    the path as the slug, e.g. http: / / site / post / the-slug,
    and look up the post id via the API.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: JSON response dict with 'id' and other fields
    """
    auth_entity = self.auth_entity.get()
    logging.info('Determining WordPress.com post id for %s', post_url)

    # extract the post's slug and look up its post id
    path = urlparse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      logging.info('Looking up post id for slug %s', slug)
      url = API_POST_SLUG_URL % (auth_entity.blog_id, slug.encode('utf-8'))
      resp = auth_entity.urlopen(url).read()
      post_id = json.loads(resp).get('ID')
      if not post_id:
        return self.error('Could not find post id')

    logging.info('Post id is %d', post_id)

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.blog_id, post_id)
    content = u'<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    data = {'content': content.encode('utf-8')}
    try:
      resp = auth_entity.urlopen(url, data=urllib.urlencode(data)).read()
    except urllib2.HTTPError, e:
      body = e.read()
      parsed = json.loads(body) if body else {}
      if e.code == 400 and parsed.get('error') == 'invalid_input':
        return parsed  # known error: https://github.com/snarfed/bridgy/issues/161
      raise

    resp = json.loads(resp)
    resp['id'] = resp.pop('ID', None)
    return resp


class AddWordPress(oauth_wordpress.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(WordPress, auth_entity, state)


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = WordPress


application = webapp2.WSGIApplication([
    ('/wordpress/start', oauth_wordpress.StartHandler.to('/wordpress/add')),
    # This handles both add and delete. (WordPress.com only allows a single
    # OAuth redirect URL.)
    ('/wordpress/add', AddWordPress),
    ('/wordpress/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
