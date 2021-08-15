"""IndieAuth handlers for authenticating and proving ownership of a domain.
"""
from google.cloud import ndb
from oauth_dropins import indieauth

from models import Domain
import util
from util import redirect


class Start(indieauth.Start):
  """Serves the "Enter your web site" form page; starts the IndieAuth flow."""
  def template_file(self):
    return 'indieauth.html'

  def template_vars(self):
    return {
      'token': flask_util.get_required_param('token'),
      **super().template_vars(),
    }

  def post(self):
    try:
      return redirect(redirect_url(state=flask_util.get_required_param('token')))
    except Exception as e:
      if util.is_connection_failure(e) or util.interpret_http_exception(e)[0]:
        flash("Couldn't fetch your web site: %s" % e)
        return redirect('/')
      raise


class Callback(indieauth.Callback):
  """IndieAuth callback handler."""
  @ndb.transactional()
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      return

    assert state
    domain = Domain.get_or_insert(util.domain_from_link(
      util.replace_test_domains_with_localhost(auth_entity.key.id())))
    domain.auth = auth_entity.key
    if state not in domain.tokens:
      domain.tokens.append(state)
    domain.put()

    flash(f'Authorized you for {domain.key.id()}.')
    return redirect('/')


# ROUTES = [
#   ('/indieauth/start', Start.to('/indieauth/callback')),
#   ('/indieauth/callback', Callback),
# ]
