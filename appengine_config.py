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
else:
  DEBUG = True
  MOCKFACEBOOK = True
