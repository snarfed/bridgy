"""Mastodon source and datastore model classes."""
from datetime import datetime
import logging

from flask import request
from granary import mastodon as gr_mastodon
from granary import source as gr_source
from oauth_dropins import mastodon as oauth_mastodon
from oauth_dropins.webutil.flask_util import flash
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

from flask_app import app
import models
import util

logger = logging.getLogger(__name__)

# https://docs.joinmastodon.org/api/oauth-scopes/
LISTEN_SCOPES = (
  'read:accounts',
  'read:blocks',
  'read:notifications',
  'read:search',
  'read:statuses',
)
PUBLISH_SCOPES = LISTEN_SCOPES + (
  'write:statuses',
  'write:favourites',
  'write:media',
)
SCOPE_SEPARATOR = oauth_mastodon.Start.SCOPE_SEPARATOR


class StartBase(oauth_mastodon.Start):
  """Abstract base OAuth starter class with our redirect URLs."""
  DEFAULT_SCOPE = ''
  REDIRECT_PATHS = (
    '/mastodon/callback',
    '/publish/mastodon/finish',
    '/micropub-token/mastodon/finish',
    '/mastodon/delete/finish',
    '/delete/finish',
  )
  # https://github.com/snarfed/bridgy/issues/1344
  EXPIRE_APPS_BEFORE = datetime(2022, 9, 26)

  def app_name(self):
    return 'Bridgy'

  def app_url(self):
    return util.host_url()


class Mastodon(models.Source):
  """A Mastodon account.

  The key name is the fully qualified address, eg
  ``@snarfed@mastodon.technology``.
  """
  GR_CLASS = gr_mastodon.Mastodon
  OAUTH_START = StartBase
  SHORT_NAME = 'mastodon'
  CAN_PUBLISH = True
  AUTH_MODEL = oauth_mastodon.MastodonAuth
  MICROPUB_TOKEN_PROPERTY = 'access_token_str'
  HAS_BLOCKS = True
  TYPE_LABELS = {
    'post': 'toot',
    'comment': 'reply',
    'repost': 'boost',
    'like': 'favorite',
  }
  DISABLE_HTTP_CODES = ('401', '403', '404')
  USERNAME_KEY_ID = True

  @property
  def URL_CANONICALIZER(self):
    """Generated dynamically to use the instance's domain."""
    return util.UrlCanonicalizer(domain=self.gr_source.DOMAIN)

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`Mastodon` entity.

    Args:
      auth_entity (oauth_mastodon.MastodonAuth):
      kwargs: property values
    """
    assert 'username' not in kwargs
    assert 'id' not in kwargs
    user = json_loads(auth_entity.user_json)
    return Mastodon(username=auth_entity.key_id(),
                    auth_entity=auth_entity.key,
                    url=user.get('url'),
                    name=user.get('display_name') or user.get('username'),
                    picture=user.get('avatar'),
                    **kwargs)

  def instance(self):
    """Returns the Mastodon instance domain, e.g. ``foo.com`` ."""
    return self._split_address()[1]

  def _split_address(self):
    split = self.key_id().split('@')
    assert len(split) == 3 and split[0] == '', self.key_id()
    return split[1], split[2]

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. ``tag:foo.com:alice``."""
    username = self._split_address()[0]
    return self.gr_source.tag_uri(username)

  def silo_url(self):
    """Returns the Mastodon profile URL, e.g. https://foo.com/@bar."""
    return json_loads(self.auth_entity.get().user_json).get('url')

  def label_name(self):
    """Returns the fully qualified address."""
    return self.username

  @classmethod
  def button_html(cls, feature, **kwargs):
    """Override oauth-dropins's to not show the instance text box."""
    source = kwargs.get('source')
    instance = source.instance() if source else ''
    scopes = SCOPE_SEPARATOR.join(
      PUBLISH_SCOPES if 'publish' in feature else LISTEN_SCOPES)
    return f"""\
<form method="{'post' if instance else 'get'}" action="/mastodon/start">
  <input type="image" class="mastodon-button shadow" alt="Sign in with Mastodon"
         src="/oauth_dropins_static/mastodon_large.png" />
  <input name="feature" type="hidden" value="{feature}" />
  <input name="instance" type="hidden" value="{instance}" />
  <input name="scope" type="hidden" value="{scopes}" />
</form>
"""

  def load_blocklist(self):
    try:
      return super().load_blocklist()
    except requests.HTTPError as e:
      if e.response.status_code == 403:
        # this user signed up before we started asking for the 'follow' OAuth
        # scope, which the block list API endpoint requires. just skip them.
        # https://console.cloud.google.com/errors/CMfA_KfIld6Q2AE
        logger.info("Couldn't fetch block list due to missing OAuth scope")
        self.blocked_ids = []
        self.put()
      else:
        raise


@app.route('/mastodon/start', methods=['GET'])
def enter_your_instance():
  """Serves the "Enter your instance" form page."""
  return util.render_template('mastodon_instance.html')


class Start(StartBase):
  def redirect_url(self, *args, **kwargs):
    features = (request.form.get('feature') or '').split(',')
    starter = util.oauth_starter(StartBase)('/mastodon/callback',
      scopes=PUBLISH_SCOPES if 'publish' in features else LISTEN_SCOPES)

    try:
      return starter.redirect_url(*args, instance=request.form['instance'], **kwargs)
    except (ValueError, requests.HTTPError) as e:
      logger.warning('Bad Mastodon instance', exc_info=True)
      flash(util.linkify(str(e), pretty=True))
      util.redirect(request.path)


class Callback(oauth_mastodon.Callback):
  def finish(self, auth_entity, state=None):
    logger.debug(f'finish with {auth_entity}, {state}')
    util.maybe_add_or_delete_source(Mastodon, auth_entity, state)


app.add_url_rule('/mastodon/start',
                 view_func=Start.as_view('mastodon_start', '/mastodon/callback'), methods=['POST'])
app.add_url_rule('/mastodon/callback', view_func=Callback.as_view('mastodon_callback', 'unused'))
app.add_url_rule('/mastodon/delete/finish',
                 view_func=oauth_mastodon.Callback.as_view('mastodon_delete_finish', '/delete/finish'))
app.add_url_rule('/mastodon/publish/start',
                 view_func=StartBase.as_view('mastodon_publish_finish', '/publish/mastodon/finish', scopes=PUBLISH_SCOPES), methods=['POST'])
