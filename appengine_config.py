"""bridgy App Engine app.

App Engine config settings.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

from google.appengine import dist
dist.use_library('django', '1.2')

import os
if os.environ['SERVER_SOFTWARE'].startswith('Development'):
  DEBUG = True
  MOCKFACEBOOK = True
else:
  DEBUG = False
  MOCKFACEBOOK = False
