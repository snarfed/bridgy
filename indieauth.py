"""IndieAuth handlers for authenticating and proving ownership of a domain.
"""
import uuid

from google.cloud import ndb
from oauth_dropins import indieauth
from oauth_dropins.webutil.handlers import TemplateHandler

from models import Domain
import util


class StartHandler(TemplateHandler, indieauth.StartHandler, util.Handler):
  """Serves the "Enter your web site" form page; starts the IndieAuth flow."""
  def template_file(self):
    return 'indieauth.html'

  def template_vars(self):
    return {
      'token': util.get_required_param(self, 'token'),
      **super().template_vars(),
    }

  def post(self):
    try:
      self.redirect(self.redirect_url(state=util.get_required_param(self, 'token')))
    except Exception as e:
      if util.is_connection_failure(e) or util.interpret_http_exception(e)[0]:
        self.messages.add("Couldn't fetch your web site: %s" % e)
        return self.redirect('/')
      raise


class CallbackHandler(indieauth.CallbackHandler, util.Handler):
  """IndieAuth callback handler."""
  @ndb.transactional()
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      return

    assert state
    domain = Domain.get_or_insert(auth_entity.key.id())
    domain.auth = auth_entity.key
    if state not in domain.tokens:
      domain.tokens.append(state)
    domain.put()

    self.messages.add(f'Authorized you for {domain.key.id()}.')
    self.redirect('/')


ROUTES = [
  ('/indieauth/start', StartHandler.to('/indieauth/callback')),
  ('/indieauth/callback', CallbackHandler),
]
