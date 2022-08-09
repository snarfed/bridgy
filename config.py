"""Flask config.

https://flask.palletsprojects.com/en/latest/config/
"""
from oauth_dropins.webutil import appengine_info, util

if appengine_info.DEBUG:
  ENV = 'development'
  CACHE_TYPE = 'NullCache'
  SECRET_KEY = 'sooper seekret'
else:
  ENV = 'production'
  CACHE_TYPE = 'SimpleCache'
  SECRET_KEY = util.read('flask_secret_key')
