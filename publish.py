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
from facebook import FacebookPage
from googleplus import GooglePlusPage
from instagram import Instagram
from mf2py import parser
import models
import requests
from twitter import Twitter
import util
import webapp2

from google.appengine.api import mail
from google.appengine.ext import ndb
from google.appengine.ext.webapp import template

SOURCES = {cls.SHORT_NAME: cls for cls in
           (FacebookPage, Twitter, Instagram, GooglePlusPage)}


class Handler(util.Handler):
  """Base handler for both previews and webmentions.

  Subclasses must set the PREVIEW attribute to True or False.
  """
  PREVIEW = None

  def post(self):
    self.source = None
    self.publish = None
    logging.info('Params: %self', self.request.params.items())

    source_url = util.get_required_param(self, 'source')
    target_url = util.get_required_param(self, 'target')

    assert self.PREVIEW in (True, False)
    if not self.PREVIEW:
      self.response.headers['Content-Type'] = 'application/json'

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
    elif source_cls in (Instagram, GooglePlusPage):
      return self.error('Sorry, %s is not yet supported.' %
                        source_cls.AS_CLASS.NAME)

    # validate, fetch, and parse source
    msg = 'Could not parse source URL %s' % source_url
    try:
      parsed = urlparse.urlparse(source_url)
    except BaseException:
      return self.error(msg)
    domain = parsed.netloc
    if not domain:
      return self.error(msg)

    # When debugging locally, use snarfed.org for localhost webmentions
    if appengine_config.DEBUG and domain == 'localhost':
      domain = 'snarfed.org'

    # look up source by domain
    self.source = (source_cls.query()
                   .filter(source_cls.domain == domain)
                   .filter(source_cls.features == 'publish')
                   .get())
    if not self.source:
      return self.error("Could not find %(type)s account for %(domain)s. Check that you're signed up for Bridgy Publish and that your %(type)s account has %(domain)s in its profile's 'web site' or 'link' field." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    if not self.PREVIEW:
      self.add_publish_entity(source_url)

    # fetch source URL
    try:
      resp = requests.get(source_url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    except BaseException:
      return self.error('Could not fetch source URL %s' % source_url)

    # parse microformats, convert to ActivityStreams
    if not self.PREVIEW:
      self.publish.html = resp.text
    data = parser.Parser(doc=resp.text).to_dict()
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

    # if we're responding to a silo object, it should match the requested silo
    _, base_url = self.source.as_source.base_object(obj)
    if base_url:
      try:
        domain = urlparse.urlparse(base_url).netloc
        if domain.startswith('www.'):
          domain = domain[4:]
        if domain != self.source.AS_CLASS.DOMAIN:
          return self.error('Could not find %s link in %s' %
                            (self.source.AS_CLASS.NAME, source_url))
      except BaseException:
        msg = 'Could not parse link %s' % base_url
        logging.exception(msg)
        return self.error(msg)

    # add original post link to end of content
    if obj.get('content'):
      obj['content'] += ((' %s' if source_cls == Twitter else '\n\n(%s)') %
                         source_url)

    try:
      if self.PREVIEW:
        preview_text = self.source.as_source.preview_create(obj)
      else:
        self.publish.published = self.source.as_source.create(obj)
    except NotImplementedError:
      types = items[0].get('type', [])
      if 'h-entry' in types:
        types.remove('h-entry')
      return self.error("%s doesn't support type(s) %s." %
                        (source_cls.AS_CLASS.NAME, ' + '.join(types)),
                        data=data, log_exception=False)
    except BaseException, e:
      return self.error('Error: %s' % e, status=500)

    if self.PREVIEW:
      vars = {'source': self.preprocess_source(self.source),
              'preview': preview_text,
              'source_url': source_url,
              'target_url': target_url,
              'webmention_endpoint': self.request.host_url + '/publish/webmention',
              }
      self.response.write(template.render('templates/preview.html', vars))
      self.mail_me(preview_text, 'preview succeeded')
      return

    # we've actually created something in the silo. write results to datastore.
    if 'url' not in self.publish.published:
      self.publish.published['url'] = obj.get('url')
    self.publish.status = 'complete'
    self.publish.type = models.get_type(obj)
    self.publish.put()

    resp = json.dumps(self.publish.published, indent=2)
    self.mail_me(resp, 'succeeded')
    self.response.write(resp)

  def error(self, error, status=400, data=None, log_exception=True):
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)
    self.response.set_status(status)
    label = 'failed'
    if self.PREVIEW:
      error = util.linkify(error)
      label = 'preview failed'
    self.response.write(error)
    self.mail_me(error, label)

  def mail_me(self, resp, result):
    subject = 'Bridgy publish %s' % result
    body = 'Request:\n%s\n\nResponse:\n%s' % (self.request.params.items(), resp)

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
    logging.debug('Publish entity: %s', self.publish.key.urlsafe())


class PreviewHandler(Handler):
  """Renders a preview HTML snippet of how a webmention would be handled.
  """
  PREVIEW = True


class WebmentionHandler(Handler):
  """Accepts webmentions and translates them to site-specific API calls.
  """
  PREVIEW = False

  def error(self, error, status=400, data=None, log_exception=True):
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)

    if self.publish:
      self.publish.status = 'failed'
      self.publish.put()

    self.response.set_status(status)
    resp = {'error': error}
    if data:
      resp['parsed'] = data

    resp = json.dumps(resp, indent=2)
    self.mail_me(resp, 'preview failed' if self.PREVIEW else 'failed')
    self.response.write(resp)


class WebmentionLinkHandler(webapp2.RequestHandler):
  """Returns the Base handler for both previews and webmentions.

  Subclasses must set the PREVIEW attribute to True or False.
  """
  def head(self, site):
    self.response.headers['Link'] = (
      '<%s/publish/webmention>; rel="webmention"' % self.request.host_url)

  def get(self, site):
    self.head(site)
    self.response.out.write("""\
<!DOCTYPE html>
<html><head>
<link rel="webmention" href="%s/publish/webmention">
</head>
<body>Nothing here! Try <a href="%s/publish">%s/publish</a>.</body>
<html>""" % (self.request.host_url, self.request.host_url, appengine_config.HOST))


application = webapp2.WSGIApplication([
    ('/publish/webmention', WebmentionHandler),
    ('/publish/preview', PreviewHandler),
    ('/publish/(facebook|twitter)', WebmentionLinkHandler),
    ],
  debug=appengine_config.DEBUG)
