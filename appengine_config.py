"""bridgy App Engine app.

App Engine config settings.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

try:
  from google.appengine import dist
  dist.use_library('django', '1.2')
except ImportError:
  # python2.7 runtime doesn't have google.appengine.dist
  pass

import os
if not os.environ.get('SERVER_SOFTWARE', '').startswith('Development'):
  DEBUG = False
  MOCKFACEBOOK = False
  # separate prod and devel google app ids because google only includes
  # refresh_token in the *first* oauth response, even across redirect URLs (e.g.
  # localhost vs brid.gy), so if localhost gets it, brid.gy doesn't and can't
  # refresh a token after it expires.
  GOOGLE_CLIENT_ID = '1029605954231.apps.googleusercontent.com'
  GOOGLE_CLIENT_SECRET_FILE = 'google_client_secret_bridgy'
else:
  DEBUG = True
  MOCKFACEBOOK = False
  GOOGLE_CLIENT_ID = '581979435635.apps.googleusercontent.com'
  GOOGLE_CLIENT_SECRET_FILE = 'google_client_secret_bridgy_devel'
