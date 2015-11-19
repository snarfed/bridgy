"""Bridgy App Engine config.
"""
import os

# Load packages from virtualenv
# https://cloud.google.com/appengine/docs/python/tools/libraries27#vendoring
from google.appengine.ext import vendor
vendor.add('local')

from granary.appengine_config import *

DISQUS_ACCESS_TOKEN = read('disqus_access_token')
DISQUS_API_KEY = read('disqus_api_key')
DISQUS_API_SECRET = read('disqus_api_secret')
FACEBOOK_TEST_USER_TOKEN = (os.getenv('FACEBOOK_TEST_USER_TOKEN') or
                            read('facebook_test_user_access_token'))
SUPERFEEDR_TOKEN = read('superfeedr_token')
SUPERFEEDR_USERNAME = read('superfeedr_username')

# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from oauth_dropins.webutil import util
util._orig_tag_uri = util.tag_uri
util.tag_uri = lambda domain, name: util._orig_tag_uri(domain, name, year=2013)

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


def webapp_add_wsgi_middleware(app):
  # # uncomment for app stats
  # appstats_CALC_RPC_COSTS = True
  # from google.appengine.ext.appstats import recording
  # app = recording.appstats_wsgi_middleware(app)

  # # uncomment for instance_info concurrent requests recording
  # from oauth_dropins.webutil import instance_info
  # app = instance_info.concurrent_requests_wsgi_middleware(app)

  return app
