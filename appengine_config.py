"""Bridgy App Engine config.
"""
import logging
import os

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

# Suppress warnings. These are duplicated in oauth-dropins and bridgy; keep them
# in sync!
import warnings
warnings.filterwarnings('ignore', module='bs4',
                        message='No parser was explicitly specified')

# NDB client
# https://googleapis.dev/python/python-ndb/latest/
# TODO: make thread local?
# https://googleapis.dev/python/python-ndb/latest/migrating.html#setting-up-a-connection
from google.cloud import ndb
ndb_client = ndb.Client()

if DEBUG:
  # HACK! work around that the python 3 ndb lib doesn't support dev_appserver.py
  # https://github.com/googleapis/python-ndb/issues/238
  ndb_client.host = 'localhost:8081'
  ndb_client.secure = False
