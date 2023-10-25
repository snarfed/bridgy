import models
from granary import bluesky as gr_bluesky
from oauth_dropins import bluesky as oauth_bluesky
from flask_app import app
import util
import logging
from flask import flash, render_template
from oauth_dropins.webutil.util import json_loads
from urllib.parse import quote

logger = logging.getLogger(__name__)


class Bluesky(models.Source):
  """
  A Bluesky account.
  """
  SHORT_NAME = 'bluesky'
  GR_CLASS = gr_bluesky.Bluesky
  OAUTH_START = oauth_bluesky.Start
  AUTH_MODEL = oauth_bluesky.BlueskyAuth
  URL_CANONICALIZER = util.UrlCanonicalizer(
          domain=GR_CLASS.DOMAIN,
          # Bluesky does not support HEAD requests.
          redirects=False)

  @staticmethod
  def new(auth_entity, **kwargs):
    """Creates and returns a :class:`Bluesky` entity.

    Args:
      auth_entity: :class:`oauth_bluesky.BlueskyAuth`
      kwargs: property values
    """
    assert 'username' not in kwargs
    assert 'id' not in kwargs
    user = json_loads(auth_entity.user_json)
    (_, app_password) = auth_entity.access_token()
    gr_source = gr_bluesky.Bluesky(user.get('handle'), app_password=app_password)
    actor = gr_source.user_to_actor(user)
    return Bluesky(id=auth_entity.key_id(),
                   username=user.get('handle'),
                   auth_entity=auth_entity.key,
                   name=user.get('displayName'),
                   picture=user.get('avatar'),
                   url=actor.get('url'),
                   **kwargs)

  def silo_url(self):
    """Returns the Bluesky account URL, e.g. https://bsky.app/profile/foo.bsky.social."""
    return self.gr_source.user_url(self.username)

  def label_name(self):
    """Returns the Bluesky handle."""
    return self.username

  def format_for_source_url(self, id):
    """
    Bluesky keys (AT URIs) contain slashes, so must be double-encoded.
    This is due to a particular behaviour in WSGI: https://github.com/pallets/flask/issues/900
    They do not need to be decoded correspondingly.
    """
    return quote(quote(id, safe=''))

  @classmethod
  def button_html(cls, feature):
    """Override oauth-dropins's button_html() to not show the instance text box."""
    return f"""\
<form method="get" action="/bluesky/start">
  <input type="image" class="shadow" height="50" title="Bluesky"
         src="/oauth_dropins_static/bluesky_2x.png" />
  <input name="feature" type="hidden" value="{feature}" />
</form>
"""

class Callback(oauth_bluesky.Callback):
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      flash("Failed to log in to Bluesky. Are your credentials correct?")
      return util.redirect("/bluesky/start")
    if auth_entity:
      util.maybe_add_or_delete_source(
        Bluesky,
        auth_entity,
        util.construct_state_param_for_add(),
      )

@app.route('/bluesky/start', methods=['GET'])
def provide_app_password():
  """Serves the Bluesky login form page."""
  return render_template('provide_app_password.html')


app.add_url_rule('/bluesky/callback', view_func=Callback.as_view('bluesky_callback', 'unused'), methods=['POST'])
