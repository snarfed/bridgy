"""Bridgy App Engine config.
"""

from google.appengine.api import namespace_manager

from activitystreams.appengine_config import *

# prefer brid.gy to brid-gy.appspot.com
if HOST and HOST.endswith('brid-gy.appspot.com'):
  HOST = 'www.brid.gy'
  SCHEME = 'https'

# I used a namespace for a while when I had both versions deployed, but not any
# more; I cleared out the old v1 datastore entities.
# Called only if the current namespace is not set.
# def namespace_manager_default_namespace_for_request():
#   return 'webmention-dev'

# uncomment for app stats
# def webapp_add_wsgi_middleware(app):
#   from google.appengine.ext.appstats import recording
#   app = recording.appstats_wsgi_middleware(app)
#   return app
