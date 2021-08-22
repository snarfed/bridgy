"""Bridgy background flask app, mostly task queue handlers: poll, propagate, etc."""
from flask import Flask
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.appengine_config import ndb_client

import appengine_config


# Flask app
app = Flask('background')
app.config.from_pyfile('config.py')
# XXX TODO background_handle_exception
app.register_error_handler(Exception, flask_util.handle_exception)
app.wsgi_app = flask_util.ndb_context_middleware(app.wsgi_app, client=ndb_client)


@app.route('/_ah/<any(start, stop, warmup):_>')
def noop(_):
  return 'OK'
