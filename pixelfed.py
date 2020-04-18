"""Pixelfed source and datastore model classes."""
from granary import pixelfed as gr_pixelfed
import oauth_dropins.pixelfed
import webapp2

import mastodon
import util


class StartHandler(oauth_dropins.pixelfed.StartHandler):
  """Abstract base OAuth starter class with our redirect URLs."""
  REDIRECT_PATHS = (
    '/pixelfed/callback',
    # TODO: uncomment when https://github.com/pixelfed/pixelfed/issues/2106 is fixed
    # '/publish/pixelfed/finish',
    # '/pixelfed/delete/finish',
    # '/delete/finish',
  )

  def app_name(self):
    return 'Bridgy'

  def app_url(self):
    if self.request.host in util.OTHER_DOMAINS:
      return util.HOST_URL

    return super().app_url()


class Pixelfed(mastodon.Mastodon):
  """A Pixelfed account.

  The key name is the fully qualified address, eg '@snarfed@piconic.co'.
  """
  GR_CLASS = gr_pixelfed.Pixelfed
  OAUTH_START_HANDLER = StartHandler
  SHORT_NAME = 'pixelfed'
  CAN_PUBLISH = True
  HAS_BLOCKS = False
  TYPE_LABELS = GR_CLASS.TYPE_LABELS

  def search_for_links(self):
    return []

class InstanceHandler(mastodon.InstanceHandler):
  """Serves the "Enter your instance" form page."""
  SITE = 'pixelfed'
  START_HANDLER = StartHandler

  # https://docs.pixelfed.org/technical-documentation/api-v1.html
  LISTEN_SCOPES = ('read',)
  PUBLISH_SCOPES = LISTEN_SCOPES + ('write',)

  def template_vars(self):
    return {
      'site': self.SITE,
      'logo_file': 'pixelfed_logo.png',
      'join_url': 'https://pixelfed.org/join',
    }


class CallbackHandler(oauth_dropins.pixelfed.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    source = self.maybe_add_or_delete_source(Pixelfed, auth_entity, state)


ROUTES = [
  ('/pixelfed/start', InstanceHandler),
  ('/pixelfed/callback', CallbackHandler),
  ('/pixelfed/delete/finish', oauth_dropins.pixelfed.CallbackHandler.to('/delete/finish')),
  ('/pixelfed/publish/start', StartHandler.to(
    '/publish/pixelfed/finish', scopes=InstanceHandler.PUBLISH_SCOPES)),
]
