"""Mastodon source and datastore model classes."""
from __future__ import unicode_literals

import logging

import appengine_config
from granary import mastodon as gr_mastodon
from granary import source as gr_source
from oauth_dropins import mastodon as oauth_mastodon
from oauth_dropins.webutil.handlers import TemplateHandler
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
import webapp2

import models
import util

# https://docs.joinmastodon.org/api/permissions/
LISTEN_SCOPES = ('read', 'follow')
PUBLISH_SCOPES = LISTEN_SCOPES + ('write',)


class StartHandler(oauth_mastodon.StartHandler):
  """Abstract base OAuth starter class with our redirect URLs."""
  APP_NAME = 'Bridgy'
  APP_URL = (util.HOST_URL if appengine_config.HOST in util.OTHER_DOMAINS
             else appengine_config.HOST_URL)
  REDIRECT_PATHS = (
    '/mastodon/callback',
    '/publish/mastodon/finish',
    '/mastodon/delete/finish',
    '/delete/finish',
  )


class Mastodon(models.Source):
  """A Mastodon account.

  The key name is the fully qualified address, eg 'snarfed@mastodon.technology'.
  """
  GR_CLASS = gr_mastodon.Mastodon
  OAUTH_START_HANDLER = StartHandler
  SHORT_NAME = 'mastodon'
  CAN_PUBLISH = True
  HAS_BLOCKS = True
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

  @classmethod
  def button_html(cls, feature, **kwargs):
    """Override oauth-dropins's button_html() to not show the instance text box."""
    source = kwargs.get('source')
    instance = source.instance() if source else ''
    return """\
<form method="%s" action="/mastodon/start">
  <input type="image" class="mastodon-button shadow" alt="Sign in with Mastodon"
         src="/oauth_dropins/static/mastodon_large.png" />
  <input name="feature" type="hidden" value="%s" />
  <input name="instance" type="hidden" value="%s" />
</form>
""" % ('post' if instance else 'get', feature, instance)

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
    if not self.domains:
      return []

    query = ' OR '.join(self.domains)
    return self.get_activities(
      search_query=query, group_id=gr_source.SEARCH, fetch_replies=False,
      fetch_likes=False, fetch_shares=False)

  def is_blocked(self, obj):
    try:
      return super(Mastodon, self).is_blocked(obj)
    except requests.HTTPError as e:
      if e.response.status_code == 403:
        # this user signed up before we started asking for the 'follow' OAuth
        # scope, which the block list API endpoint requires. just skip them.
        # https://console.cloud.google.com/errors/CMfA_KfIld6Q2AE
        logging.info("Couldn't fetch block list due to missing OAuth scope")
        return False
      raise


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
    try:
      self.redirect(start.redirect_url(instance=instance))
    except ValueError as e:
      logging.warning('Bad Mastodon instance', exc_info=True)
      self.messages.add(util.linkify(unicode(e), pretty=True))
      return self.redirect(self.request.path)



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
