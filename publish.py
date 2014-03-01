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

curl -d 'source=http://localhost/bridgy_publish.html&target=http://brid.gy/publish/twitter' http://localhost:8080/publish/webmention
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
import models
import requests
import twitter
import webapp2

from google.appengine.api import mail
from google.appengine.ext import ndb

SOURCES = {cls.SHORT_NAME: cls for cls in
           (facebook.FacebookPage,
            googleplus.GooglePlusPage,
            instagram.Instagram,
            twitter.Twitter)}
SUPPORTED_SOURCES = {facebook.FacebookPage, twitter.Twitter}


class WebmentionHandler(webapp2.RequestHandler):
  """Accepts webmentions and translates them to site-specific API calls.
  """

  def post(self):
    """Handles an API GET.

    Request path is of the form /user_id/group_id/app_id/activity_id , where
    each element is an optional string object id.
    """
    self.source = None
    self.publish = None

    self.response.headers['Content-Type'] = 'application/json'
    logging.info('Params: %s', self.request.params)

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

    if source_cls not in SUPPORTED_SOURCES:
      return self.error('Sorry, %s is not yet supported.' % source_cls.AS_CLASS.NAME)

    # validate, fetch, and parse source
    try:
      parsed = urlparse.urlparse(source_url)
    except BaseException:
      return self.error('Could not parse source URL %s' % source_url)

    domain = parsed.netloc
    if not domain:
      return self.error('Could not parse source URL %s' % source_url)

    # When debugging locally, use snarfed.org for localhost webmentions
    if appengine_config.DEBUG and domain == 'localhost':
      domain = 'snarfed.org'

    # look up source by domain
    source = self.source = source_cls.query().filter(source_cls.domain == domain).get()
    if not source:
      return self.error(
        "Could not find %(type)s account for %(domain)s. Check that you're signed up "
        "for Bridgy and that your %(type)s account has %(domain)s in its profile's "
        "'web site' or 'link' field." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    self.add_publish_entity(source_url)

    # try:
    #   resp = requests.get(source, allow_redirects=True, timeout=HTTP_TIMEOUT)
    # except BaseException:
    #   return self.error(msg, 'Could not fetch source URL %s' % source)
    # TODO(timeout)
    # data = parser.Parser(file=StringIO.StringIO(resp.text)).to_dict()

    # TODO: fetch myself? mf2py doesn't work when I give it a StringIO though.
    data = parser.Parser(source_url).to_dict()
    logging.debug('Parsed microformats2: %s', data)
    items = data.get('items', [])
    if not items or not items[0]:
      return self.error('No microformats2 data found in %s' % source_url,
                        data=data)

    obj = microformats2.json_to_object(items[0])
    logging.debug('Converted to ActivityStreams object: %s', obj)

    # posts and comments need content
    if obj.get('objectType') in ('note', 'article', 'comment'):
      contents = items[0].get('properties', {}).get('content', [])
      if not contents or not contents[0] or not contents[0].get('value'):
        return self.error('Could not find e-content in %s' % source_url, data=data)

    # add original post link to end of content
    # TODO: make prettier?
    if obj.get('content'):
      obj['content'] += '\n\n(%s)' % source_url

    try:
      resp = source.as_source.create(obj)
    except NotImplementedError:
      return self.error("%s doesn't support type(s) %s." %
                        (source_cls.AS_CLASS.NAME, items[0].get('type')),
                        data=data, log_exception=False)

    # write results to datastore
    self.publish.status = 'complete'
    # TODO
    self.publish.type = models.get_type(obj)
    self.publish.html = 'TODO'
    self.publish.published_id = resp.get('id')
    self.publish.published_url = resp.get('url')
    self.publish.put()

    self.mail_me(resp, True)
    self.response.write(json.dumps(resp))

  def error(self, error, status=400, data=None, log_exception=True):
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)

    if self.publish:
      # TODO: more details
      self.publish.status = 'failed'
      self.publish.put()

    self.response.set_status(status)
    resp = {'error': error}
    if data:
      resp['parsed'] = data

    self.mail_me(resp, False)
    self.response.write(json.dumps(resp))

  def mail_me(self, resp, success):
    subject = 'Bridgy publish %s' % ('succeeded' if success else 'failed')
    body = 'Request:\n%s\n\nResponse:\n%s' % \
        (self.request.params, json.dumps(resp))

    if self.source:
      prefix = 'Source: %s/publish#%s\n\n' % (self.request.host_url,
                                              self.source.dom_id())
      body = prefix + body
      subject += ': %s' % self.source.label()

    mail.send_mail(sender='publish@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', subject=subject, body=body)

  @ndb.transactional
  def add_publish_entity(self, source_url):
    """Creates and stores Publish and (if necessary) PublishedPage entities.

    Args:
      source_url: string
    """
    page = models.PublishedPage.get_or_insert(source_url)
    self.publish = models.Publish(parent=page.key, source=self.source.key)
    self.publish.put()


application = webapp2.WSGIApplication([
    ('/publish/webmention', WebmentionHandler),
    ],
  debug=appengine_config.DEBUG)
