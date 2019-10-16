"""Mastodon source and datastore model classes."""
from __future__ import unicode_literals

import logging

import appengine_config
from granary import mastodon as gr_mastodon
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
  def URL_CANONICALIZER():
    """Generate URL_CANONICALIZER dynamically to use the instance's domain."""
    return util.UrlCanonicalizer(
      domain=GR_CLASS.DOMAIN,
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

  def instance(self):
    """Returns the Mastodon instance URL, e.g. https://foo.com/."""
    return self.auth_entity.get().instance()

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


class StartHandler(TemplateHandler, util.Handler):
  """Serves the "Enter your instance" form page."""

  def template_file(self):
    return 'mastodon_instance.html'

  def post(self):
    feature = self.request.get('feature')
    start = util.oauth_starter(oauth_mastodon.StartHandler).to(
      '/mastodon/callback', app_name='Bridgy', app_url=appengine_config.HOST_URL,
      scopes=PUBLISH_SCOPES if feature == 'publish' else LISTEN_SCOPES)(
      self.request, self.response)

    instance = util.get_required_param(self, 'instance')
    self.redirect(start.redirect_url(instance=instance))


class CallbackHandler(oauth_mastodon.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    source = self.maybe_add_or_delete_source(Mastodon, auth_entity, state)


application = webapp2.WSGIApplication([
  ('/mastodon/start', StartHandler),
  ('/mastodon/callback', CallbackHandler),
  # TODO
  # ('/mastodon/delete/finish', oauth_mastodon.CallbackHandler.to('/delete/finish')),
  ('/mastodon/publish/start', oauth_mastodon.StartHandler.to(
    '/publish/mastodon/finish', app_name='Bridgy', app_url=appengine_config.HOST_URL,
    scopes=PUBLISH_SCOPES)),
], debug=appengine_config.DEBUG)
