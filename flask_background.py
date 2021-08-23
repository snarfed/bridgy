"""Bridgy background flask app, mostly task queue handlers: poll, propagate, etc."""
import logging

from flask import Flask, g
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.appengine_config import ndb_client
from werkzeug.exceptions import HTTPException

import appengine_config, util


# Flask app
app = Flask('background')
app.config.from_pyfile('config.py')
app.wsgi_app = flask_util.ndb_context_middleware(app.wsgi_app, client=ndb_client)


@app.errorhandler(Exception)
def background_handle_exception(e):
  """Common exception handler for background tasks.

  Catches failed outbound HTTP requests and returns HTTP 304.
  """
  if isinstance(e, HTTPException):
    # raised by this app itself, pass it through
    return str(e), e.code

  transients = getattr(g, 'TRANSIENT_ERROR_HTTP_CODES', ())
  source = getattr(g, 'source', None)
  if source:
    transients += source.RATE_LIMIT_HTTP_CODES + source.TRANSIENT_ERROR_HTTP_CODES

  code, body = util.interpret_http_exception(e)
  if ((code and int(code) // 100 == 5) or code in transients or
      util.is_connection_failure(e)):
    logging.error(f'Marking as error and finishing. {code}: {body}\n{e}')
    return '', util.ERROR_HTTP_RETURN_CODE

  raise e


@app.route('/_ah/<any(start, stop, warmup):_>')
def noop(_):
  return 'OK'
