"""Micropub API to publish.

Micropub spec: https://www.w3.org/TR/micropub/
"""
import logging

from flask import jsonify, request
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil import appengine_info
from oauth_dropins.webutil.util import json_dumps, json_loads
from werkzeug.exceptions import HTTPException

from flask_app import app
from flickr import Flickr
from github import GitHub
from oauth_dropins.flickr import FlickrAuth
from oauth_dropins.github import GitHubAuth
from oauth_dropins.mastodon import MastodonAuth
from oauth_dropins.twitter import TwitterAuth
from mastodon import Mastodon
from models import Publish
import models
from publish import PublishBase
from twitter import Twitter
import util
import webmention

logger = logging.getLogger(__name__)

SOURCE_CLASSES = (
  (Twitter, TwitterAuth, TwitterAuth.token_secret),
  (Mastodon, MastodonAuth, MastodonAuth.access_token_str),
  (GitHub, GitHubAuth, GitHubAuth.access_token_str),
  (Flickr, FlickrAuth, FlickrAuth.token_secret),
)
RESERVED_PARAMS = ('access_token', 'action', 'q', 'url')
RESERVED_PREFIX = 'mp-'


def form_to_mf2(params):
  # this should really be k.removesuffix('[]'), but removesuffix is new in
  # Python 3.9, and Circle is on 3.7 right now
  return {k.rstrip('[]'): v for k, v in params.items()
          if k not in RESERVED_PARAMS and not k.startswith(RESERVED_PREFIX)}


class Micropub(PublishBase):
  """Micropub endpoint."""

  def error(self, error, description, **kwargs):
    return super().error(error=error,
                         extra_json={'error_description': description},
                         **kwargs)

  def load_source(self):
    """Looks up the auth entity by the provided access token."""
    auth = request.headers.get('Authorization')
    if auth:
      parts = auth.split(' ')
      if len(parts) != 2 or parts[0] != 'Bearer':
        return self.error('invalid_request',
                          'Unsupported token format in Authorization header',
                          status=401)
      token = parts[1]
    else:
      token = request.values.get('access_token')

    if not token:
      return self.error('unauthorized',
                        'No token found in Authorization header or access_token param',
                        status=401)

    for src_cls, auth_cls, prop in SOURCE_CLASSES:
      auth_entity = auth_cls.query(prop == token).get()
      if auth_entity:
        return src_cls.query(src_cls.auth_entity == auth_entity.key).get()

    return self.error('unauthorized', 'No user found with that token', status=401)

  def dispatch_request(self):
    logging.info(f'Params: {list(request.values.items())}')

    # Micropub query; currently only config is supported
    q = request.values.get('q')
    if q == 'config':
      return jsonify({})
    elif q:
      return self.error('not_implemented', 'Only config query is supported')

    # now we assume it's a create/update/delete
    if request.method != 'POST':
      return self.error('invalid_request',
                        'Expected POST for Micropub create/delete',
                        status=405)

    self.source = self.load_source()
    if self.source.status == 'disabled' or 'publish' not in self.source.features:
      return self.error('forbidden',
                        f'Publish is not enabled for {self.source.label()}',
                        status=403)

    # handle input
    if request.is_json:
      mf2 = request.json
      action = mf2.get('action')
      url = mf2.get('url')
    elif request.form:
      mf2 = {
        'h': request.form.get('h') or 'entry',
        'properties': form_to_mf2(request.form.to_dict(flat=False)),
      }
      action = request.form.get('action')
      url = request.form.get('url')
    elif request.files:
      return self.error('not_implemented',
                        'Multipart/file upload is not yet supported')
    else:
      return self.error('invalid_request',
                        f'Unsupported Content-Type {request.content_type}')

    if not action:
      action = 'create'
    if action not in ('create', 'delete'):
      return self.error('not_implemented', f'Action {action} not supported')

    logging.debug(f'Got microformats2: {json_dumps(mf2, indent=2)}')
    obj = microformats2.json_to_object(mf2)
    logging.debug(f'Converted to ActivityStreams object: {json_dumps(obj, indent=2)}')

    canonicalized = self.source.URL_CANONICALIZER(url) or ''
    post_id = self.source.gr_source.post_id(canonicalized)
    if action == 'delete':
      if not url:
        return self.error('invalid_request', 'url is required for delete')
      elif not canonicalized:
        return self.error(
          'invalid_request',
          f"{url} doesn't look like a {self.source.gr_source.NAME} post URL")
      elif not post_id:
        return self.error(
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
      return self.error('failed', result.error_plain)

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


app.add_url_rule('/micropub', view_func=Micropub.as_view('micropub'), methods=['GET', 'POST'])
