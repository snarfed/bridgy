"""Publishes webmentions into the silos.

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

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import urllib2

import appengine_config
import webapp2

from activitystreams.oauth_dropins.webutil import util


class Handler(webapp2.RequestHandler):
  """Accepts webmentions and translates them to site-specific API calls.
  """

  def post(self):
    """Handles an API GET.

    Request path is of the form /user_id/group_id/app_id/activity_id , where
    each element is an optional string object id.
    """
    source = util.get_required_param(self, 'source')
    target = util.get_required_param(self, 'target')
    url = util.add_query_params('http://pin13.net/mf2/', {'url': source})
    urllib2.urlopen(url, timeout=appengine_config.HTTP_TIMEOUT)
    self.abort(403, 'Sorry, not implemented yet.')


application = webapp2.WSGIApplication([
    ('.*', Handler),
    ],
  debug=appengine_config.DEBUG)
