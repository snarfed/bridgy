"""Mastodon source and datastore model classes."""
from __future__ import unicode_literals

import logging

import appengine_config
from granary import mastodon as gr_mastodon
from granary import source as gr_source
from oauth_dropins import mastodon as oauth_mastodon
from oauth_dropins.webutil.handlers import TemplateHandler
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import util

# https://docs.joinmastodon.org/api/permissions/
LISTEN_SCOPES = ('read')
PUBLISH_SCOPES = ('read', 'write')


class Mastodon(models.Source):
  """A Mastodon account.

  The key name is the fully qualified address, eg 'snarfed@mastodon.technology'.
  """
  GR_CLASS = gr_mastodon.Mastodon
  SHORT_NAME = 'mastodon'
  TYPE_LABELS = {
    'post': 'toot',
    'comment': 'reply',
    'repost': 'boost',
    'like': 'favorite',
  }

  @property
  def URL_CANONICALIZER(self):
    """Generate URL_CANONICALIZER dynamically to use the instance's domain."""
    return util.UrlCanonicalizer(
      domain=self.gr_source.DOMAIN,
      headers=util.REQUEST_HEADERS)

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a :class:`Mastodon` entity.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.mastodon.MastodonAuth`
      kwargs: property values
    """
    user = json_loads(auth_entity.user_json)
    return Mastodon(id=auth_entity.key.id(),
                    auth_entity=auth_entity.key,
                    url=user.get('url'),
                    name=user.get('display_name') or user.get('username'),
                    picture=user.get('avatar'),
                    **kwargs)

  def username(self):
    """Returns the Mastodon username, e.g. alice."""
    return self._split_address()[0]

  def instance(self):
    """Returns the Mastodon instance URL, e.g. https://foo.com/."""
    return self._split_address()[1]

  def _split_address(self):
    split = self.key.id().split('@')
    assert len(split) == 3 and split[0] == '', self.key.id()
    return split[1], split[2]

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:foo.com:alice'."""
    return self.gr_source.tag_uri(self.username())

  def silo_url(self):
    """Returns the Mastodon profile URL, e.g. https://foo.com/@bar."""
    return json_loads(self.auth_entity.get().user_json).get('url')

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def is_private(self):
    """Returns True if this Mastodon account is protected.

    https://docs.joinmastodon.org/usage/privacy/#account-locking
    https://docs.joinmastodon.org/api/entities/#account
    """
    return json_loads(self.auth_entity.get().user_json).get('locked')

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    Returns:
      sequence of ActivityStreams activity dicts
    """
    query = ' OR '.join(self.domains)
    return self.get_activities(
      search_query=query, group_id=gr_source.SEARCH, fetch_replies=False,
      fetch_likes=False, fetch_shares=False)


class StartHandler(oauth_mastodon.StartHandler):
  """Abstract base OAuth starter class with our redirect URLs."""
  APP_NAME = 'Bridgy'
  APP_URL = (util.HOST_URL if appengine_config.HOST in util.OTHER_DOMAINS
             else appengine_config.HOST_URL)
  REDIRECT_PATHS = (
    '/mastodon/callback',
    '/publish/mastodon/finish',
    '/delete/finish',
  )


class InstanceHandler(TemplateHandler, util.Handler):
  """Serves the "Enter your instance" form page."""
  def template_file(self):
    return 'mastodon_instance.html'

  def post(self):
    feature = self.request.get('feature')
    start_cls = util.oauth_starter(StartHandler).to('/mastodon/callback',
      scopes=PUBLISH_SCOPES if feature == 'publish' else LISTEN_SCOPES)
    start = start_cls(self.request, self.response)

    instance = util.get_required_param(self, 'instance')
    self.redirect(start.redirect_url(instance=instance))


class CallbackHandler(oauth_mastodon.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    source = self.maybe_add_or_delete_source(Mastodon, auth_entity, state)


application = webapp2.WSGIApplication([
  ('/mastodon/start', InstanceHandler),
  ('/mastodon/callback', CallbackHandler),
  ('/mastodon/delete/finish', oauth_mastodon.CallbackHandler.to('/delete/finish')),
  ('/mastodon/publish/start', StartHandler.to('/publish/mastodon/finish',
                                              scopes=PUBLISH_SCOPES)),
], debug=appengine_config.DEBUG)
