"""WordPress REST API (including WordPress.com) hosted blog implementation.

To use, go to your WordPress.com blog's admin console, then go to Appearance,
Widgets, add a Text widget, and put this in its text section::

    <a href="https://brid.gy/webmention/wordpress" rel="webmention"></a>

Not this, it breaks::

    <link rel="webmention" href="https://brid.gy/webmention/wordpress">

https://developer.wordpress.com/docs/api/

Create returns id, can lookup by id.

Test command line::

    curl localhost:8080/webmention/wordpress -d 'source=http://localhost/response.html&target=http://ryandc.wordpress.com/2013/03/24/mac-os-x/'

Making an API call with an access token from the command line::

    curl -H 'Authorization: Bearer [TOKEN]' URL...
"""
import collections
import logging
import urllib.request, urllib.parse, urllib.error

from flask import request
from google.cloud import ndb
from oauth_dropins import wordpress_rest as oauth_wordpress
from oauth_dropins.webutil.flask_util import error, flash
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
import models
import superfeedr
import util
from util import redirect

logger = logging.getLogger(__name__)


API_CREATE_COMMENT_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/%d/replies/new?pretty=true'
API_POST_SLUG_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/slug:%s?pretty=true'
API_SITE_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s?pretty=true'


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the blog hostname.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='WordPress.com')
  OAUTH_START = oauth_wordpress.Start
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
  def new(auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      auth_entity (oauth_dropins.wordpress_rest.WordPressAuth):
    """
    site_info = WordPress.get_site_info(auth_entity)
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

  def urls_and_domains(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: unused

    Returns:
      ([str url], [str domain]) tuple:
    """
    return [self.url], [self.key_id()]

  def create_comment(self, post_url, author_name, author_url, content):
    r"""Creates a new comment in the source silo.

    If the last part of the post URL is numeric, e.g.
    ``http://site/post/123999``\, it's used as the post id. Otherwise, we
    extract the last part of the path as the slug, e.g.
    ``http://site/post/the-slug``\, and look up the post id via the API.

    Args:
      post_url (str)
      author_name (str)
      author_url (str)
      content (str)

    Returns:
      dict: JSON response with ``id`` and other fields
    """
    auth_entity = self.auth_entity.get()
    logger.info(f'Determining WordPress.com post id for {post_url}')

    # extract the post's slug and look up its post id
    path = urllib.parse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      logger.info(f'Looking up post id for slug {slug}')
      url = API_POST_SLUG_URL % (auth_entity.blog_id, slug)
      post_id = self.urlopen(auth_entity, url).get('ID')
      if not post_id:
        return error('Could not find post id')

    logger.info(f'Post id is {post_id}')

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.blog_id, post_id)
    content = f'<a href="{author_url}">{author_name}</a>: {content}'
    data = {'content': content.encode()}
    try:
      resp = self.urlopen(auth_entity, url, data=urllib.parse.urlencode(data))
    except urllib.error.HTTPError as e:
      code, body = util.interpret_http_exception(e)
      try:
        parsed = json_loads(body) if body else {}
        if code == '400' and parsed.get('error') == 'invalid_token':
          self.status = 'disabled'
          self.put()
          return error('User is disabled', status=401)
        elif ((code == '400' and parsed.get('error') == 'invalid_input') or
            (code == '403' and parsed.get('message') == 'Comments on this post are closed')):
          return parsed  # known error: https://github.com/snarfed/bridgy/issues/161
      except ValueError:
        pass  # fall through
      raise e

    resp['id'] = resp.pop('ID', None)
    return resp

  @classmethod
  def get_site_info(cls, auth_entity):
    """Fetches the site info from the API.

    Args:
      auth_entity (oauth_dropins.wordpress_rest.WordPressAuth)

    Returns:
      dict: site info, or None if API calls are disabled for this blog
    """
    try:
      return cls.urlopen(auth_entity, API_SITE_URL % auth_entity.blog_id)
    except urllib.error.HTTPError as e:
      code, body = util.interpret_http_exception(e)
      if (code == '403' and '"API calls to this blog have been disabled."' in body):
        flash(f'You need to <a href="http://jetpack.me/support/json-api/">enable the Jetpack JSON API</a> in {util.pretty_link(auth_entity.blog_url)}\'s WordPress admin console.')
        redirect('/')
        return None
      raise

  @staticmethod
  def urlopen(auth_entity, url, **kwargs):
    resp = auth_entity.urlopen(url, **kwargs).read()
    logger.debug(resp)
    return json_loads(resp)


class Add(oauth_wordpress.Callback):
  """This handles both add and delete.

  (WordPress.com only allows a single OAuth redirect URL.)
  """
  def finish(self, auth_entity, state=None):
    if auth_entity:
      if int(auth_entity.blog_id) == 0:
        flash('Please try again and choose a blog before clicking Authorize.')
        return redirect('/')

      # Check if this is a self-hosted WordPress blog
      site_info = WordPress.get_site_info(auth_entity)
      if site_info is None:
        return
      elif site_info.get('jetpack'):
        logger.info(f'This is a self-hosted WordPress blog! {auth_entity.key_id()} {auth_entity.blog_id}')
        return util.render_template('confirm_self_hosted_wordpress.html',
                                    auth_entity_key=auth_entity.key.urlsafe().decode(),
                                    state=state)

    util.maybe_add_or_delete_source(WordPress, auth_entity, state)


@app.route('/wordpress/confirm', methods=['POST'])
def confirm_self_hosted():
  util.maybe_add_or_delete_source(
    WordPress,
    ndb.Key(urlsafe=request.form['auth_entity_key']).get(),
    request.form['state'])


class SuperfeedrNotify(superfeedr.Notify):
  SOURCE_CLS = WordPress


# wordpress.com doesn't seem to use scope
# https://developer.wordpress.com/docs/oauth2/
start = util.oauth_starter(oauth_wordpress.Start).as_view(
  'wordpress_start', '/wordpress/add')
app.add_url_rule('/wordpress/start', view_func=start, methods=['POST'])
app.add_url_rule('/wordpress/add', view_func=Add.as_view('wordpress_add', 'unused'))
app.add_url_rule('/wordpress/notify/<id>', view_func=SuperfeedrNotify.as_view('wordpress_notify'), methods=['POST'])
