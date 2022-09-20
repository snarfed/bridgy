"""Micropub API to publish.

Micropub spec: https://www.w3.org/TR/micropub/
"""
import logging
import urllib.request, urllib.parse, urllib.error

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
from publish import PublishBase
import models
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


def remove_reserved(params):
  return {k: v for k, v in params.items()
          if k not in RESERVED_PARAMS and not k.startswith(RESERVED_PREFIX)}


class Micropub(PublishBase):
  """Micropub endpoint."""

  def load_source(self):
    """Looks up the auth entity by the provided access token."""
    auth = request.headers.get('Authorization')
    if auth:
      parts = auth.split(' ')
      if len(parts) != 2 or parts[0] != 'Bearer':
        return self.error('Unsupported token format in Authorization header', status=401)
      token = parts[1]
    else:
      token = request.values.get('access_token')

    if not token:
      return self.error('No token found in Authorization header or access_token param',
                        status=401)

    for src_cls, auth_cls, prop in SOURCE_CLASSES:
      auth_entity = auth_cls.query(prop == token).get()
      if auth_entity:
        return src_cls.query(src_cls.auth_entity == auth_entity.key).get()

    return self.error('No user found with that token', status=401)

  def dispatch_request(self):
    logging.info(f'Params: {list(request.values.items())}')

    # Micropub query; currently only config is supported
    q = request.values.get('q')
    if q == 'config':
      return jsonify({})
    elif q:
      return self.error(error='not_implemented')

    self.source = self.load_source()

    # handle input
    if request.is_json:
      mf2 = request.json
    elif request.form:
      mf2 = {
        'h': request.values.get('h') or 'entry',
        'properties': remove_reserved(request.form.to_dict()),
      }
    elif request.files:
      pass
    else:
      return self.error(error='invalid_request', extra_json={
        'error_description': f'Unsupported Content-Type {request.content_type}',
      })

    obj = microformats2.json_to_object(mf2)
    logging.debug(f'Converted to ActivityStreams object: {json_dumps(obj, indent=2)}')

    # TODO: is this the right idea to require mf2 url so I can de-dupe?
    url = request.values.get('url') or util.get_url(obj)
    assert url

    # done with the sanity checks, create the Publish entity
    self.entity = self.get_or_add_publish_entity(url)
    if not self.entity:  # get_or_add_publish_entity() populated the error response
      return

    # check that we haven't already published this URL
    if self.entity.status == 'complete' and not appengine_info.LOCAL:
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't support updating existing posts. Details: https://github.com/snarfed/bridgy/issues/84",
                        extra_json={'original': self.entity.published})

    # TODO: convert form-encoded, multipart to JSON

    self.preprocess(obj)

    result = self.source.gr_source.create(obj)
    logger.info(f'Result: {result}')
    if result.error_plain:
      return self.error(result.error_plain)

    self.entity.published = result.content
    if 'url' not in self.entity.published:
      self.entity.published['url'] = obj.get('url')
    self.entity.type = self.entity.published.get('type') or models.get_type(obj)

    # except HTTPException:
    #   # raised by us, probably via self.error()
    #   raise
    # except BaseException as e:
    #   code, body = util.interpret_http_exception(e)
    #   if code in self.source.DISABLE_HTTP_CODES or isinstance(e, models.DisableSource):
    #     # the user deauthorized the bridgy app, or the token expired, so
    #     # disable this source.
    #     logging.warning(f'Disabling source due to: {e}', exc_info=True)
    #     self.source.status = 'disabled'
    #     self.source.put()
    #   if isinstance(e, (NotImplementedError, ValueError, urllib.error.URLError)):
    #     code = '400'
    #   elif not code:
    #     raise
    #   msg = f"Error: {body or ''} {e}"
    #   return self.error(msg, status=code, report=code not in
    #                     ('400', '403', '404', '406', '502', '503', '504'))

    # write results to datastore
    self.entity.status = 'complete'
    self.entity.put()

    return '', 201, {'Location': self.entity.published['url']}


app.add_url_rule('/micropub', view_func=Micropub.as_view('micropub'), methods=['GET', 'POST'])
