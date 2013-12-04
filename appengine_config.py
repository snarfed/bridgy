"""Bridgy App Engine config.
"""

from google.appengine.api import namespace_manager

from activitystreams.appengine_config import *

# I used a namespace for a while when I had both versions deployed, but not any
# more; I cleared out the old v1 datastore entities.
# Called only if the current namespace is not set.
# def namespace_manager_default_namespace_for_request():
#   return 'webmention-dev'
