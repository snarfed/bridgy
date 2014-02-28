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

Test cmd line:

curl -d 'source=http://localhost/bridgy_fb_post.html&target=http://brid.gy/publish/facebook' http://localhost:8080/publish/webmention
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import StringIO
import sys
import urllib2
import urlparse
from webob import exc

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams import microformats2
from activitystreams.oauth_dropins.webutil import util
import facebook
import googleplus
import instagram
from mf2py import parser
import requests
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

    source_url = util.get_required_param(self, 'source')
    target_url = util.get_required_param(self, 'target')

    # parse and validate target URL
    try:
      parsed = urlparse.urlparse(target_url)
    except BaseException:
      return self.error(msg, 'Could not parse target URL %s' % target_url)

    domain = parsed.netloc
    path_parts = parsed.path.rsplit('/', 1)
    source_cls = SOURCES.get(path_parts[-1])
    if (domain not in ('brid.gy', 'www.brid.gy') or len(path_parts) != 2 or
        path_parts[0] != '/publish' or not source_cls):
      return self.error('Target must be brid.gy/publish/{facebook,twitter}')

    if source_cls not in (facebook.FacebookPage, twitter.Twitter):
      return self.error('Sorry, %s is not yet supported.' % source_cls.AS_CLASS.NAME)

    # validate, fetch, and parse source
    try:
      parsed = urlparse.urlparse(source_url)
    except BaseException:
      return self.error(msg, 'Could not parse source URL %s' % source)

    domain = parsed.netloc
    # When debugging locally, use snarfed.org for localhost webmentions
    if appengine_config.DEBUG and domain == 'localhost':
      domain = 'snarfed.org'

    # look up source by domain
    source = source_cls.query().filter(source_cls.domain == domain).get()
    if not source:
      return self.error(
        "Could not find %(type)s account for %(domain)s. Check that you're signed up "
        "for Bridgy and that your %(type)s account has %(domain)s in in its profile's "
        "'web site' or 'link' field ." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    # try:
    #   resp = requests.get(source, allow_redirects=True, timeout=HTTP_TIMEOUT)
    # except BaseException:
    #   return self.error(msg, 'Could not fetch source URL %s' % source)
    # TODO(timeout)
    # data = parser.Parser(file=StringIO.StringIO(resp.text)).to_dict()

    # TODO: fetch myself? mf2py doesn't work when I give it a StringIO though.
    data = parser.Parser(source_url).to_dict()
    logging.info('@ %s', data)
    items = data.get('items', [])
    if not items or not items[0]:
      self.error('No mf2 data found in %s. Found: %s', source_url, data)

    contents = items[0].get('properties', {}).get('content', [])
    if not contents or not contents[0] or not contents[0].get('value'):
      self.error('Could not find e-content in %s. Found: %s' % (source_url, data))

    obj = microformats2.json_to_object(items[0])
    # TODO: make prettier?
    obj['content'] += '\n\n%s' % source_url
    logging.info('@ %s', obj)

    # resp = source.as_source.create(obj)
    # self.response.write(json.dumps(resp))

  def error(self, error, status=400):
    logging.error(error, exc_info=sys.exc_info())
    self.response.set_status(status)
    self.response.write(json.dumps({'error': error}))


application = webapp2.WSGIApplication([
    ('/publish/webmention', WebmentionHandler),
    ],
  debug=appengine_config.DEBUG)
