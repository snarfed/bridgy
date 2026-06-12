"""Bridgy user-facing views: front page, user pages, and delete POSTs.
"""
from pathlib import Path
import string
import sys

from flask import Flask
import flask_gae_static
import humanize
from werkzeug.middleware.proxy_fix import ProxyFix
from webutil import flask_util
from webutil.appengine_config import ndb_client
from webutil import appengine_info

import granary
import appengine_config  # *after* import granary to override set_user_agent()
import models
import util


# Flask app
app = Flask(__name__, static_folder=None)
app.template_folder = './templates'
app.json.compact = False
app.config.from_pyfile(Path(__file__).parent / 'config.py')
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
app.before_request(flask_util.canonicalize_domain(
  util.OTHER_DOMAINS, util.PRIMARY_DOMAIN))
if appengine_info.LOCAL_SERVER and not appengine_info.TESTING:
  flask_gae_static.init_app(app)

app.wsgi_app = flask_util.ndb_context_middleware(app.wsgi_app, client=ndb_client)

# make Flask's request info (request.url etc) reflect the actual end user's HTTP
# request (https, host, etc), based on X-Forwarded-* etc headers
#
# https://docs.cloud.google.com/functions/docs/reference/headers
# https://werkzeug.palletsprojects.com/en/stable/middleware/proxy_fix/
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1)

app.jinja_env.globals.update({
  'naturaltime': util.naturaltime,
  'get_logins': util.get_logins,
  'sources': models.sources,
  'string': string,
  'util': util,
  'EPOCH': util.EPOCH,
})


@app.route('/_ah/<any(start, stop, warmup):_>')
def noop(_):
  return 'OK'
