"""WordPress REST API (including WordPress.com) hosted blog implementation.

To use, go to your WordPress.com blog's admin console, then go to Appearance,
Widgets, add a Text widget, and put this in its text section:

<a href="https://brid.gy/webmention/wordpress" rel="webmention"></a>

(not this, it breaks :/)
<link rel="webmention" href="https://brid.gy/webmention/wordpress">

https://developer.wordpress.com/docs/api/
create returns id, can lookup by id

test command line:
curl localhost:8080/webmention/wordpress \
  -d 'source=http://localhost/response.html&target=http://ryandc.wordpress.com/2013/03/24/mac-os-x/'

making an API call with an access token from the command line:
curl -H 'Authorization: Bearer [TOKEN]' URL...
"""
from __future__ import unicode_literals
from future.moves.urllib import error as urllib_error_py2

from future import standard_library
standard_library.install_aliases()
import collections
import logging
import urllib.request, urllib.parse, urllib.error

import appengine_config

from google.cloud import ndb
from oauth_dropins import wordpress_rest as oauth_wordpress
from oauth_dropins.webutil.handlers import JINJA_ENV
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import superfeedr
import util


API_CREATE_COMMENT_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/%d/replies/new?pretty=true'
API_POST_SLUG_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/slug:%s?pretty=true'
API_SITE_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s?pretty=true'


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the blog hostname.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='WordPress.com')
  OAUTH_START_HANDLER = oauth_wordpress.StartHandler
  SHORT_NAME = 'wordpress'

  site_info = ndb.JsonProperty(compressed=True)  # from /sites/$site API call

  def feed_url(self):
    # http://en.support.wordpress.com/feeds/
    return urllib.parse.urljoin(self.silo_url(), 'feed/')

  def silo_url(self):
    return self.domain_urls[0]

  def edit_template_url(self):
    return urllib.parse.urljoin(self.silo_url(), 'wp-admin/widgets.php')

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.wordpress_rest.WordPressAuth`
    """
    site_info = WordPress.get_site_info(handler, auth_entity)
    if site_info is None:
      return

    urls = util.dedupe_urls(util.trim_nulls(
      [site_info.get('URL'), auth_entity.blog_url]))
    domains = [util.domain_from_link(u) for u in urls]

    avatar = (json_loads(auth_entity.user_json).get('avatar_URL')
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

  def _urls_and_domains(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: unused

    Returns:
      ([string url], [string domain])
    """
    return [self.url], [self.key.id()]

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

    Returns:
      JSON response dict with 'id' and other fields
    """
    auth_entity = self.auth_entity.get()
    logging.info('Determining WordPress.com post id for %s', post_url)

    # extract the post's slug and look up its post id
    path = urllib.parse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      logging.info('Looking up post id for slug %s', slug)
      url = API_POST_SLUG_URL % (auth_entity.blog_id, slug)
      post_id = self.urlopen(auth_entity, url).get('ID')
      if not post_id:
        return self.error('Could not find post id', report=False)

    logging.info('Post id is %d', post_id)

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.blog_id, post_id)
    content = '<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    data = {'content': content.encode('utf-8')}
    try:
      resp = self.urlopen(auth_entity, url, data=urllib.parse.urlencode(data))
    except (urllib.error.HTTPError, urllib_error_py2.HTTPError) as e:
      code, body = util.interpret_http_exception(e)
      try:
        parsed = json_loads(body) if body else {}
        if ((code == '400' and parsed.get('error') == 'invalid_input') or
            (code == '403' and parsed.get('message') == 'Comments on this post are closed')):
          return parsed  # known error: https://github.com/snarfed/bridgy/issues/161
      except ValueError:
        pass # fall through
      raise e

    resp['id'] = resp.pop('ID', None)
    return resp

  @classmethod
  def get_site_info(cls, handler, auth_entity):
    """Fetches the site info from the API.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.wordpress_rest.WordPressAuth`

    Returns:
      site info dict, or None if API calls are disabled for this blog
    """
    try:
      return cls.urlopen(auth_entity, API_SITE_URL % auth_entity.blog_id)
    except (urllib.error.HTTPError, urllib_error_py2.HTTPError) as e:
      code, body = util.interpret_http_exception(e)
      if (code == '403' and '"API calls to this blog have been disabled."' in body):
        handler.messages.add(
          'You need to <a href="http://jetpack.me/support/json-api/">enable '
          'the Jetpack JSON API</a> in %s\'s WordPress admin console.' %
          util.pretty_link(auth_entity.blog_url))
        handler.redirect('/')
        return None
      raise

  @staticmethod
  def urlopen(auth_entity, url, **kwargs):
    resp = auth_entity.urlopen(url, **kwargs).read()
    logging.debug(resp)
    return json_loads(resp)


class AddWordPress(oauth_wordpress.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    if auth_entity:
      if int(auth_entity.blog_id) == 0:
        self.messages.add(
          'Please try again and choose a blog before clicking Authorize.')
        return self.redirect('/')

      # Check if this is a self-hosted WordPress blog
      site_info = WordPress.get_site_info(self, auth_entity)
      if site_info is None:
        return
      elif site_info.get('jetpack'):
        logging.info('This is a self-hosted WordPress blog! %s %s',
                     auth_entity.key.id(), auth_entity.blog_id)
        self.response.headers['Content-Type'] = 'text/html'
        self.response.out.write(
          JINJA_ENV.get_template('confirm_self_hosted_wordpress.html').render(
            auth_entity_key=auth_entity.key.urlsafe(),
            state=state,
          ))
        return

    self.maybe_add_or_delete_source(WordPress, auth_entity, state)


class ConfirmSelfHosted(util.Handler):
  def post(self):
    self.maybe_add_or_delete_source(
      WordPress,
      ndb.Key(urlsafe=util.get_required_param(self, 'auth_entity_key')).get(),
      util.get_required_param(self, 'state'))


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = WordPress


application = webapp2.WSGIApplication([
    # wordpress.com doesn't seem to use scope
    # https://developer.wordpress.com/docs/oauth2/
    ('/wordpress/start', util.oauth_starter(oauth_wordpress.StartHandler).to(
      '/wordpress/add')),
    ('/wordpress/confirm', ConfirmSelfHosted),
    # This handles both add and delete. (WordPress.com only allows a single
    # OAuth redirect URL.)
    ('/wordpress/add', AddWordPress),
    ('/wordpress/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
