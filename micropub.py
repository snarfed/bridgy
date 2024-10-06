"""Micropub API to publish.

Micropub spec: https://www.w3.org/TR/micropub/
"""
import binascii
import logging

from flask import jsonify, render_template, request
from flask.views import View
from google.cloud import ndb
import google.protobuf.message
from granary import microformats2
from granary import source as gr_source
from oauth_dropins import (
  bluesky as oauth_bluesky,
  flickr as oauth_flickr,
  github as oauth_github,
  mastodon as oauth_mastodon,
)
from oauth_dropins.webutil import appengine_info
from oauth_dropins.webutil.flask_util import flash
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from werkzeug.exceptions import HTTPException

from flask_app import app
import mastodon
from models import Publish
import models
from publish import PublishBase
import util
from util import redirect
import webmention

logger = logging.getLogger(__name__)

RESERVED_PARAMS = ('access_token', 'action', 'q', 'url')
RESERVED_PREFIX = 'mp-'


def form_to_mf2(params):
  return {k.removesuffix('[]'): v for k, v in params.items()
          if k not in RESERVED_PARAMS and not k.startswith(RESERVED_PREFIX)}


class Micropub(PublishBase):
  """Micropub endpoint."""

  def error(self, error, description, **kwargs):
    super().error(error=error,
                  extra_json={'error_description': description},
                  **kwargs)

  def load_source(self):
    """Looks up the auth entity by the provided access token."""
    auth = request.headers.get('Authorization')
    if auth:
      parts = auth.split(' ')
      if len(parts) != 2 or parts[0] != 'Bearer':
        self.error('invalid_request',
                   'Unsupported token format in Authorization header',
                   status=401)
      token = parts[1]
    else:
      token = request.values.get('access_token')

    if not token:
      self.error('unauthorized',
                 'No token found in Authorization header or access_token param',
                 status=401)

    for src_cls in models.sources.values():
      if src_cls.CAN_PUBLISH:
        token_prop = getattr(src_cls.AUTH_MODEL, src_cls.MICROPUB_TOKEN_PROPERTY)
        auth_entity = src_cls.AUTH_MODEL.query(token_prop == token).get()
        if auth_entity:
          src = src_cls.query(src_cls.auth_entity == auth_entity.key,
                              src_cls.status == 'enabled',
                              src_cls.features == 'publish',
                              ).get()
          if src:
            return src

    self.error('unauthorized', 'No publish user found with that token', status=401)

  def dispatch_request(self):
    logger.info(f'Params: {list(request.values.items())}')

    # auth
    self.source = self.load_source()
    logger.info(f'Source: {self.source.label()} {self.source.key_id()}, {self.source.bridgy_url()}')
    if self.source.status == 'disabled' or 'publish' not in self.source.features:
      self.error('forbidden',
                 f'Publish is not enabled for {self.source.label()}',
                 status=403)

    # Micropub query; currently only config is supported
    q = request.values.get('q')
    if q == 'config':
      return jsonify({})
    elif q:
      self.error('not_implemented', 'Only config query is supported')

    if request.method == 'GET':
      return render_template('micropub.html')
    elif request.method != 'POST':
      self.error('invalid_request',
                 'Expected POST for Micropub create/delete',
                 status=405)

    # handle input
    if request.is_json:
      logger.info('Got JSON input')
      mf2 = request.json
      action = mf2.get('action')
      url = mf2.get('url')
    elif request.form:
      logger.info('Got form-encoded input')
      mf2 = {
        'h': request.form.get('h') or 'entry',
        'properties': form_to_mf2(request.form.to_dict(flat=False)),
      }
      action = request.form.get('action')
      url = request.form.get('url')
    elif request.files:
      self.error('not_implemented',
                 'Multipart/file upload is not yet supported')
    else:
      self.error('invalid_request',
                 f'Unsupported Content-Type {request.content_type}')

    if not action:
      action = 'create'
    if action not in ('create', 'delete'):
      self.error('not_implemented', f'Action {action} not supported')

    logger.debug(f'Got microformats2: {json_dumps(mf2, indent=2)}')
    try:
      obj = microformats2.json_to_object(mf2)
    except (TypeError, ValueError, KeyError) as e:
      self.error('invalid_request', f'Invalid microformats2 input: {e}')

    # override articles to be notes to force short-form granary sources like
    # Mastodon to use content, not displayName
    if obj.get('objectType') == 'article':
      obj['objectType'] = 'note'
    logger.debug(f'Converted to ActivityStreams object: {json_dumps(obj, indent=2)}')

    canonicalized = self.source.URL_CANONICALIZER(url or '') or ''
    post_id = self.source.gr_source.post_id(canonicalized)
    if action == 'delete':
      if not url:
        self.error('invalid_request', 'url is required for delete')
      elif not canonicalized:
        self.error('invalid_request',
                   f"{url} doesn't look like a {self.source.gr_source.NAME} post URL")
      elif not post_id:
        self.error(
          'invalid_request',
          f"Couldn't determine {self.source.gr_source.NAME} post id from {url}")

    # done with validation, start publishing
    self.preprocess(obj)
    type = 'delete' if action == 'delete' else None
    self.entity = Publish(source=self.source.key, mf2=mf2, type=type)
    self.entity.put()

    if action == 'create':
      result = self.source.gr_source.create(obj)
    else:
      assert action == 'delete'
      assert post_id
      result = self.source.gr_source.delete(post_id)

    logger.info(f'Result: {result}')
    if result.error_plain:
      self.entity.status = 'failed'
      self.entity.put()
      self.error('failed', result.error_plain)

    self.entity.published = result.content
    self.entity.type = self.entity.published.get('type') or models.get_type(obj)
    self.entity.put()

    # write results to datastore
    self.entity.status = 'complete' if action == 'create' else 'deleted'
    self.entity.put()

    url = self.entity.published.get('url')
    if action == 'create':
      return result.content, 201, ({'Location': url} if url else {})
    else:
      return result.content, 200


class GetToken(View):
  """OAuth callback for 'Get token' button."""
  def finish(self, auth_entity, state=None):
    if not state:
      return redirect('/')

    # this somewhat duplicates util.load_source() :/
    try:
      source = ndb.Key(urlsafe=state).get()
    except (ValueError, binascii.Error, google.protobuf.message.DecodeError):
      source = None

    logger.info(f'Got source: {source}')
    if not source:
      flash(f"Bad state value, couldn't find your user")
      return redirect('/')

    if not auth_entity:
      flash('If you want a Micropub token, please approve the prompt.')
    elif not auth_entity.is_authority_for(source.auth_entity):
      flash(f'To get a Micropub token for {source.label_name()}, please log into {source.GR_CLASS.NAME} as that account.')
    else:
      token = getattr(auth_entity, source.MICROPUB_TOKEN_PROPERTY)
      flash(f'Your <a href="/about#micropub">Micropub token</a> for {source.label()} is: <code>{token}</code>')

    return redirect(source.bridgy_url())


@app.post('/micropub-token/bluesky/start', endpoint='micropub_token_bluesky_start')
def bluesky_start():
  return render_template('provide_app_password.html',
                         post_url='/micropub-token/bluesky/finish',
                         **request.values)


class MastodonStart(mastodon.StartBase):
  def dispatch_request(self):
    source = util.load_source()
    # request all scopes we currently need, since Mastodon and Pleroma scopes
    # are per access token, and oauth-dropins overwrites the auth entity with
    # the latest token. background:
    # https://github.com/snarfed/bridgy/issues/1015
    # https://github.com/snarfed/bridgy/issues/1342
    self.scope = self.SCOPE_SEPARATOR.join(
      mastodon.PUBLISH_SCOPES if 'publish' in source.features
      else mastodon.LISTEN_SCOPES)

    try:
      return super().dispatch_request()
    except (ValueError, requests.HTTPError) as e:
      logger.warning('Bad Mastodon instance', exc_info=True)
      flash(util.linkify(str(e), pretty=True))
      redirect(source.bridgy_path())


# We want Callback.get() and GetToken.finish(), so put Callback first and
# override finish.
class BlueskyToken(oauth_bluesky.Callback, GetToken):
  finish = GetToken.finish


class FlickrToken(oauth_flickr.Callback, GetToken):
  finish = GetToken.finish


class GitHubToken(oauth_github.Callback, GetToken):
  finish = GetToken.finish


class MastodonToken(oauth_mastodon.Callback, GetToken):
  finish = GetToken.finish


app.add_url_rule('/micropub', view_func=Micropub.as_view('micropub'), methods=['GET', 'POST'])

app.add_url_rule('/micropub-token/bluesky/finish', view_func=BlueskyToken.as_view('micropub_token_bluesky_finish', 'finish'), methods=['POST'])

app.add_url_rule('/micropub-token/flickr/start', view_func=oauth_flickr.Start.as_view('micropub_token_flickr_start', '/micropub-token/flickr/finish'), methods=['POST'])
app.add_url_rule('/micropub-token/flickr/finish', view_func=FlickrToken.as_view('micropub_token_flickr_finish', 'unused'))

app.add_url_rule('/micropub-token/github/start', view_func=oauth_github.Start.as_view('micropub_token_github_start', '/micropub-token/github/finish'), methods=['POST'])
app.add_url_rule('/micropub-token/github/finish', view_func=GitHubToken.as_view('micropub_token_github_finish', 'unused'))

app.add_url_rule('/micropub-token/mastodon/start', view_func=MastodonStart.as_view('micropub_token_mastodon_start', '/micropub-token/mastodon/finish'), methods=['POST'])
app.add_url_rule('/micropub-token/mastodon/finish', view_func=MastodonToken.as_view('micropub_token_mastodon_finish', 'unused'))
