"""IndieAuth handlers for authenticating and proving ownership of a domain."""
from flask import render_template, request
from google.cloud import ndb
from oauth_dropins import indieauth
from oauth_dropins.webutil.flask_util import flash

from flask_app import app
from models import Domain
import util
from util import redirect


@app.route('/indieauth/start', methods=['GET'])
def indieauth_enter_web_site():
  """Serves the "Enter your web site" form page."""
  return render_template('indieauth.html', token=request.args['token'])


class Start(indieauth.Start):
  """Starts the IndieAuth flow."""
  def dispatch_request(self):
    token = request.form['token']

    try:
      to_url = self.redirect_url(state=token)
    except Exception as e:
      if util.is_connection_failure(e) or util.interpret_http_exception(e)[0]:
        flash(f"Couldn't fetch your web site: {e}")
        return redirect('/')
      raise

    return redirect(to_url)


class Callback(indieauth.Callback):
  """IndieAuth callback handler."""
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      return

    assert state

    @ndb.transactional()
    def add_or_update_domain():
      domain = Domain.get_or_insert(util.domain_from_link(
        util.replace_test_domains_with_localhost(auth_entity.key.id())))
      domain.auth = auth_entity.key
      if state not in domain.tokens:
        domain.tokens.append(state)
      domain.put()
      flash(f'Authorized you for {domain.key.id()}.')

    add_or_update_domain()
    return redirect('/')


app.add_url_rule('/indieauth/start',
                 view_func=Start.as_view('indieauth_start', '/indieauth/callback'),
                 methods=['POST'])
app.add_url_rule('/indieauth/callback',
                 view_func=Callback.as_view('indieauth_callback', 'unused'))
