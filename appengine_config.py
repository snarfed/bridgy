"""Bridgy App Engine config.
"""
import os
os.environ.setdefault('CLOUDSDK_CORE_PROJECT', 'brid-gy')
os.environ.setdefault('DATASTORE_DATASET', 'brid-gy')
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'brid-gy')

from granary.appengine_config import *

DISQUS_ACCESS_TOKEN = read('disqus_access_token')
DISQUS_API_KEY = read('disqus_api_key')
DISQUS_API_SECRET = read('disqus_api_secret')
SUPERFEEDR_TOKEN = read('superfeedr_token')
SUPERFEEDR_USERNAME = read('superfeedr_username')

# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from oauth_dropins.webutil import util
util._orig_tag_uri = util.tag_uri
util.tag_uri = lambda domain, name: util._orig_tag_uri(domain, name, year=2013)

# Use lxml for BeautifulSoup explicitly.
util.beautifulsoup_parser = 'lxml'

# Suppress warnings. These are duplicated in oauth-dropins and bridgy; keep them
# in sync!
import warnings
warnings.filterwarnings('ignore', module='bs4',
                        message='No parser was explicitly specified')
if DEBUG:
  warnings.filterwarnings('ignore', module='google.auth',
    message='Your application has authenticated using end user credentials')

# Google API clients
if DEBUG:
  os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'fake_user_account.json'

# https://googleapis.dev/python/python-ndb/latest/
# TODO: make thread local?
# https://googleapis.dev/python/python-ndb/latest/migrating.html#setting-up-a-connection
from google.cloud import ndb
ndb_client = ndb.Client()

from google.cloud import error_reporting
error_reporting_client = error_reporting.Client()

from google.cloud import tasks_v2
tasks_client = tasks_v2.CloudTasksClient()

if DEBUG:
  # HACK! work around that these don't natively support dev_appserver.py.
  # https://github.com/googleapis/python-ndb/issues/238
  ndb_client.host = 'localhost:8089'
  ndb_client.secure = False

  error_reporting_client.host = 'localhost:9999'
  error_reporting_client.secure = False

  tasks_client.host = 'localhost:9999'
  tasks_client.secure = False
