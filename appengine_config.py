"""bridgy App Engine app.

App Engine config settings.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

from google.appengine import dist
dist.use_library('django', '1.2')

DEBUG = True
