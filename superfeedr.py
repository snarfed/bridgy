"""Superfeedr

https://superfeedr.com/users/snarfed
http://documentation.superfeedr.com/subscribers.html
http://documentation.superfeedr.com/schema.html
"""

import appengine_config
from appengine_config import HTTP_TIMEOUT

import models
import requests
from requests.auth import HTTPBasicAuth
import util
import webapp2

PUSH_API_URL = 'https://push.superfeedr.com'


def subscribe(source):
  """Subscribes to a source.

  http://documentation.superfeedr.com/subscribers.html#addingfeedswithpubsubhubbub

  Args:
    source: Blogger, Tumblr, or WordPress
  """
  data = {
    'hub.mode': 'subscribe',
    'hub.topic': url,
    'hub.callback': '/superfeedr/notify/%s/%s' % (source.SHORT_NAME, source.domain),
    'hub.secret': ,
    'hub.verify': 'sync',
    'format': 'json',
    'retrieve': 'true',
    }

  resp = requests.post(PUSH_API_URL, data=data,
                       auth=HTTPBasicAuth(appengine_config.SUPERFEEDR_USERNAME,
                                          appengine_config.SUPERFEEDR_TOKEN))


application = webapp2.WSGIApplication([
    ('/superfeedr/notify/(blogger|tumblr|wordpress)/(.+)', NotifyHandler),
    ], debug=appengine_config.DEBUG)
