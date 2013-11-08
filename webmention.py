"""Propagates webmentions into Facebook, Twitter, and Google+.

Webmention spec: http://webmention.org/

Example request:

    POST /webmention HTTP/1.1
    Host: brid.gy
    Content-Type: application/x-www-url-form-encoded

    source=http://bob.host/post-by-bob&
    target=http://facebook.com/123

Example response:

    HTTP/1.1 202 Accepted

    http://brid.gy/webmentions/222
"""

__author__ = ['Ryan Barrett <activitystreams@ryanb.org>']

import logging
import urllib2

import appengine_config
import webapp2
from webutil import util


class Handler(webapp2.RequestHandler):
  """Accepts webmentions and translates them to site-specific API calls.
  """

  def post(self):
    """Handles an API GET.

    Request path is of the form /user_id/group_id/app_id/activity_id , where
    each element is an optional string object id.
    """
    source = self.get_required_param('source')
    target = self.get_required_param('target')
    url = util.add_query_params('http://pin13.net/mf2/', {'url': source})
    urllib2.urlopen(url, timeout=999)

  def get_required_param(self, name):
    if name not in self.request.params:
      self.abort(400, 'Missing required parameter: %s' % name)
    return self.request.params[name]


application = webapp2.WSGIApplication([
    ('.*', Handler),
    ],
  debug=appengine_config.DEBUG)
