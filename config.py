"""Flask config.

https://flask.palletsprojects.com/en/latest/config/
"""
from oauth_dropins.webutil import appengine_info, util

SECRET_KEY = util.read('flask_secret_key')
JSONIFY_PRETTYPRINT_REGULAR = True

if appengine_info.DEBUG:
  ENV = 'development'
  CACHE_TYPE = 'NullCache'
else:
  ENV = 'production'
  CACHE_TYPE = 'SimpleCache'
