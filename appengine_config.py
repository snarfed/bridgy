"""Bridgy App Engine config.
"""

import socket

from activitystreams.appengine_config import *

# turn off ndb's in-process cache. i'd love to use it, but the frontends
# constantly hit the memory cap and get killed with it on.
# https://developers.google.com/appengine/docs/python/ndb/cache
from google.appengine.ext import ndb
ndb.Context.default_cache_policy = ndb.Context._cache_policy = \
    lambda ctx, key: False

# default network timeout to 60s. the G+ and Instagram APIs use httplib2, which
# honors this:
# https://github.com/jcgregorio/httplib2/blob/master/python2/httplib2/__init__.py#L853
socket.setdefaulttimeout(60)

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
