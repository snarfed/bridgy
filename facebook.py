"""Facebook API code and datastore model classes.
"""
import urllib.parse

from google.cloud import ndb
from granary import facebook as gr_facebook
from oauth_dropins import facebook as oauth_facebook

import browser
import util


class Facebook(browser.BrowserSource):
  """A Facebook account.

  The key name is the Facebook global user id.
  """
  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook'
  OAUTH_START = oauth_facebook.Start
  URL_CANONICALIZER = util.UrlCanonicalizer(
    # no reject regexp; non-private FB post URLs just 404
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    query=True,
    approve=r'https://www\.facebook\.com/[^/?]+/posts/[^/?]+$',
    headers=util.REQUEST_HEADERS)

  # blank granary Facebook object, shared across all instances
  gr_source = gr_facebook.Facebook()

  # unique name used in FB URLs, e.g. facebook.com/[username]
  username = ndb.StringProperty()

  @classmethod
  def new(cls, auth_entity=None, actor=None, **kwargs):
    """Creates and returns an entity based on an AS1 actor."""
    src = super().new(auth_entity=None, actor=actor, **kwargs)
    src.username = actor.get('username')
    return src

  @classmethod
  def key_id_from_actor(cls, actor):
    """Returns the actor's numeric_id field to use as this entity's key id.

    numeric_id is the Facebook global user id.
    """
    return actor['numeric_id']

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id or username."""
    return ndb.Key(cls, id).get() or cls.query(cls.username == id).get()

  def silo_url(self):
    """Returns the Facebook profile URL, e.g. https://facebook.com/foo.

    Facebook profile URLS with app-scoped user ids (eg www.facebook.com/ID) no
    longer work as of April 2018, so if that's all we have, return None instead.
    https://developers.facebook.com/blog/post/2018/04/19/facebook-login-changes-address-abuse/
    """
    if self.username:
      return self.gr_source.user_url(self.username)

    user_id = self.key.id()
    # STATE: define this, where is it? not here or granary or o-d
    if util.is_int(id) and int(id) < MIN_APP_SCOPED_ID:
      return self.gr_source.user_url(user_id)

  @classmethod
  def button_html(cls, feature, **kwargs):
    return super(cls, cls).button_html(feature, form_method='get', **kwargs)

  def canonicalize_url(self, url, **kwargs):
    """Facebook-specific standardization of syndicated urls.

    Canonical form is https://www.facebook.com/USERID/posts/POSTID

    Args:
      url: a string, the url of the syndicated content
      kwargs: unused

    Return:
      a string, the canonical form of the syndication url
    """
    if util.domain_from_link(url) != self.gr_source.DOMAIN:
      return None

    def post_url(id):
      return 'https://www.facebook.com/%s/posts/%s' % (self.key.id(), id)

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    url_id = self.gr_source.post_id(url)
    ids = params.get('story_fbid') or params.get('fbid')

    post_id = ids[0] if ids else url_id
    if post_id:
      url = post_url(post_id)

    url = url.replace('facebook.com/%s/' % self.username,
                      'facebook.com/%s/' % self.key.id())

    return super().canonicalize_url(url)


browser.route(Facebook)
