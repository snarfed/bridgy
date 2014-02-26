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
import json
import urllib2
import urlparse
from webob import exc

import appengine_config

from activitystreams.oauth_dropins.webutil import util
import facebook
import googleplus
import instagram
from mf2py import parser
import twitter
import webapp2

from google.appengine.api import mail

SOURCES = {cls.SHORT_NAME: cls for cls in
           (facebook.FacebookPage,
            googleplus.GooglePlusPage,
            instagram.Instagram,
            twitter.Twitter)}


class WebmentionHandler(webapp2.RequestHandler):
  """Accepts webmentions and translates them to site-specific API calls.
  """

  def post(self):
    """Handles an API GET.

    Request path is of the form /user_id/group_id/app_id/activity_id , where
    each element is an optional string object id.
    """
    self.response.headers['Content-Type'] = 'application/json'
    logging.info('Params: %s', self.request.params)
    mail.send_mail(sender='publish@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy',
                   subject='Bridgy publish: %s' % self.request.get('target'),
                   body=str(self.request.params))

    source = util.get_required_param(self, 'source')
    target = util.get_required_param(self, 'target')

    # parse and validate source URL
    try:
      parsed = urlparse.urlparse(target)
    except BaseException, e:
      msg = 'Could not parse target URL %s' % target
      logging.exception(msg, e)
      return self.error(msg)

    domain = parsed.netloc
    # src_domain = parsed.netloc
    # if src_domain.startswith('www.'):
    #   src_domain = src_domain[4:]
    # app_domain = appengine_config.HOST
    # if app_domain.startswith('www.'):
    #   app_domain = app_domain[4:]

    path_parts = parsed.path.rsplit('/', 1)
    target_cls = SOURCES.get(path_parts[-1])
    logging.info('@ %s', [domain, path_parts, target_cls])
    if (domain not in ('brid.gy', 'www.brid.gy') or len(path_parts) != 2 or
        path_parts[0] != '/publish' or not target_cls):
      return self.error('Target must be brid.gy/publish/{facebook,twitter}')

    if target_cls not in (facebook.FacebookPage, twitter.Twitter):
      return self.error('Sorry, %s is not yet supported.' % target_cls.AS_CLASS.NAME)

    # TODO: timeout? fetch myself?
    # urllib2.urlopen(url, timeout=appengine_config.HTTP_TIMEOUT)
    data = parser.Parser(url=source).to_dict()
    logging.info(str(data))

  def error(self, error, status=400):
    self.response.set_status(status)
    self.response.write(json.dumps({'error': error}))


application = webapp2.WSGIApplication([
    ('/publish/webmention', WebmentionHandler),
    ],
  debug=appengine_config.DEBUG)
