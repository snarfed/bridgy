"""Bridgy App Engine config.
"""

from google.appengine.api import namespace_manager

from activitystreams.appengine_config import *

# Called only if the current namespace is not set.
def namespace_manager_default_namespace_for_request():
  return 'webmention-dev'
