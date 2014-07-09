"""Bridgy App Engine config.
"""

from activitystreams.appengine_config import *

DISQUS_ACCESS_TOKEN = read('disqus_access_token')
DISQUS_API_KEY = read('disqus_api_key')
DISQUS_API_SECRET = read('disqus_api_secret')
SUPERFEEDR_TOKEN = read('superfeedr_token')
SUPERFEEDR_USERNAME = read('superfeedr_username')

# Add library modules directories to sys.path so they can be imported.
#
# I used to use symlinks and munge sys.modules, but both of those ended up in
# duplicate instances of modules, which caused problems. Background in
# https://github.com/snarfed/bridgy/issues/31
for path in (
  'webmention-tools',
  ):
  path = os.path.join(os.path.dirname(__file__), path)
  if path not in sys.path:
    sys.path.append(path)

# bridgy.util overrides tag_uri() from webutil.tag_uri(). import it here so we
# know that happens everywhere tag_uri() might be used.
import util

# ereporter records exceptions and emails them to me.
# https://developers.google.com/appengine/articles/python/recording_exceptions_with_ereporter
# to test, open this path:
# http://localhost:8080/_ereporter?sender=ryan@brid.gy&to=ryan@brid.gy&debug=true&date=YYYY-MM-DD
# where the date is today or tomorrow (because of UTC)
import logging
from google.appengine.ext import ereporter
ereporter_logging_handler = ereporter.register_logger()

# temporarily disabled:
# turn off ndb's in-process cache. i'd love to use it, but the frontends
# constantly hit the memory cap and get killed with it on.
# https://developers.google.com/appengine/docs/python/ndb/cache
# from google.appengine.ext import ndb
# ndb.Context.default_cache_policy = ndb.Context._cache_policy = \
#     lambda ctx, key: False

# I used a namespace for a while when I had both versions deployed, but not any
# more; I cleared out the old v1 datastore entities.
# Called only if the current namespace is not set.
# from google.appengine.api import namespace_manager
# def namespace_manager_default_namespace_for_request():
#   return 'webmention-dev'

# uncomment for app stats
# def webapp_add_wsgi_middleware(app):
#   from google.appengine.ext.appstats import recording
#   app = recording.appstats_wsgi_middleware(app)
#   return app
