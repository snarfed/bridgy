"""Instagram API code and datastore model classes.

Example post ID and links
  id: 595990791004231349 or 595990791004231349_247678460
    (suffix is user id)
  Permalink: http://instagram.com/p/hFYnd7Nha1/
  API URL: https://api.instagram.com/v1/media/595990791004231349
  Local handler path: /post/instagram/212038/595990791004231349

Example comment ID and links
  id: 595996024371549506
  No direct API URL or permalink, as far as I can tell. :/
  API URL for all comments on that picture:
    https://api.instagram.com/v1/media/595990791004231349_247678460/comments
  Local handler path:
    /comment/instagram/212038/595990791004231349_247678460/595996024371549506
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json

import appengine_config
from oauth_dropins.webutil.handlers import TemplateHandler
import webapp2

from granary import instagram as gr_instagram
from oauth_dropins import instagram as oauth_instagram
from granary.source import SELF
import models
import util


class Instagram(models.Source):
  """An Instagram account.

  The key name is the username.
  """

  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    approve=r'https://www.instagram.com/p/[^/?]+/',
    trailing_slash=True,
    headers=util.USER_AGENT_HEADER)
    # no reject regexp; non-private Instagram post URLs just 404

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a InstagramPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.instagram.InstagramAuth
    """
    user = json.loads(auth_entity.user_json)
    username = user['username']
    return Instagram(id=username,
                     auth_entity=auth_entity.key,
                     name=user['full_name'],
                     picture=user['profile_picture'],
                     url='http://instagram.com/' + username,
                     **kwargs)

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
    user = json.loads(self.auth_entity.get().user_json)
    return self.gr_source.tag_uri(user.get('id') or self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old photos."""
    kwargs.setdefault('group_id', SELF)
    if self.is_beta_user():
      kwargs.setdefault('user_id', self.key.id())
    return self.gr_source.get_activities_response(*args, **kwargs)


class StartHandler(TemplateHandler):
  """Serves the "Enter your username" form page."""
  def template_file(self):
    return 'templates/enter_instagram_username.html'


class ConfirmHandler(TemplateHandler):
  """Serves the "Is this you?" confirmation page."""
  post = TemplateHandler.get

  def template_file(self):
    return 'templates/confirm_instagram_username.html'

  def template_vars(self):
    url = self.gr_source.user_url(util.get_required_param(self, 'username'))
    html = util.urlopen(url).read()
    activities, actor = self.gr_source.html_to_activities(html)
    return {
      'activities': activities,
      'actor': actor,
    }


class AddHandler(TemplateHandler):
  pass


application = webapp2.WSGIApplication([
    ('/instagram/start', StartHandler),
    ('/instagram/confirm', ConfirmHandler),
    ('/instagram/add', AddHandler),
], debug=appengine_config.DEBUG)
