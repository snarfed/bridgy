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
from publish import PublishBase
import models
import util
import webmention

logger = logging.getLogger(__name__)


class Micropub(PublishBase):
  """Micropub endpoint."""
  def dispatch_request(self):
    logging.info(f'Params: {list(request.values.items())}')

    # TODO: look up token
    from tests import testutil
    self.source = testutil.FakeSource.query().get()

    q = request.values.get('q')
    if q == 'config':
      return jsonify({})
    elif q:
      return self.error(error='not_implemented')

    obj = microformats2.json_to_object(request.json)
    logging.debug(f'Converted to ActivityStreams object: {json_dumps(obj, indent=2)}')

    # TODO: is this the right idea to require mf2 url so I can de-dupe?
    url = util.get_url(obj)

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
