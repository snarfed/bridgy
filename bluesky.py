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
    handle = user.get('handle')
    return Bluesky(id=auth_entity.key_id(),
                   username=handle,
                   auth_entity=auth_entity.key,
                   name=user.get('displayName'),
                   picture=user.get('avatar'),
                   url=gr_bluesky.Bluesky.user_url(handle),
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

  def post_id(self, url):
    if url.startswith('at://'):
      # Bluesky can't currently resolve AT URIs containing handles,
      # even though they are technically valid. Replace it with DID.
      return url.replace(f'at://{self.username}', f'at://{self.key_id()}')
    return gr_bluesky.web_url_to_at_uri(url, did=self.key_id(), handle=self.username)

  @classmethod
  def button_html(cls, feature, **kwargs):
    """Override oauth-dropins's button_html() to send a GET."""
    return super().button_html(feature, form_method='get', **kwargs)

  def canonicalize_url(self, url, **kwargs):
    """Canonicalizes a post or object URL.

    Overrides :class:`Source`'s to convert ``staging.bsky.app`` to ``bsky.app``,
    and to convert at:// URIs to bsky.app URLs.
    """
    if url.startswith('at://'):
      url = gr_bluesky.at_uri_to_web_url(url, handle=self.username)

    url = url.replace('https://staging.bsky.app/', 'https://bsky.app/')
    return super().canonicalize_url(url)


class Callback(oauth_bluesky.Callback):
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      flash("Failed to log in to Bluesky. Are your credentials correct?")
      return util.redirect("/bluesky/start")

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
