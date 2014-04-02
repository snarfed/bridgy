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
from models import Publish, PublishedPage
import requests
from twitter import Twitter
import util
import webapp2

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

    self.source_url = util.get_required_param(self, 'source')
    self.target_url = util.get_required_param(self, 'target')

    assert self.PREVIEW in (True, False)

    # parse and validate target URL
    try:
      parsed = urlparse.urlparse(self.target_url)
    except BaseException:
      return self.error(msg, 'Could not parse target URL %s' % self.target_url)

    domain = parsed.netloc
    path_parts = parsed.path.rsplit('/', 1)
    source_cls = SOURCES.get(path_parts[-1])
    if (domain not in ('brid.gy', 'www.brid.gy') or len(path_parts) != 2 or
        path_parts[0] != '/publish' or not source_cls):
      return self.error('Target must be brid.gy/publish/{facebook,twitter}')
    elif source_cls in (Instagram, GooglePlusPage):
      return self.error('Sorry, %s is not yet supported.' %
                        source_cls.AS_CLASS.NAME)

    # resolve source URL
    url, domain, ok = util.get_webmention_target(self.source_url)
    if not ok:
      return self.error('Unsupported source URL %s' % url)
    elif not domain:
      return self.error('Could not parse source URL %s' % url)

    # When debugging locally, use snarfed.org for localhost webmentions
    if appengine_config.DEBUG and domain == 'localhost':
      domain = 'snarfed.org'

    # look up source by domain
    self.source = (source_cls.query()
                   .filter(source_cls.domain == domain)
                   .filter(source_cls.features == 'publish')
                   .get())
    if not self.source:
      return self.error("Could not find <b>%(type)s</b> account for <b>%(domain)s</b>. Check that your %(type)s profile has %(domain)s in its <em>web site</em> or <em>link</em> field, then try signing up again." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    entity = self.get_or_add_publish_entity(url)
    if (entity.status == 'complete' and entity.type != 'preview' and
        not self.PREVIEW and not appengine_config.DEBUG):
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't yet support updating or deleting existing posts. Ping Ryan if you want that feature!")
    self.publish = entity

    # fetch source URL
    try:
      self.fetched = requests.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    except BaseException:
      return self.error('Could not fetch source URL %s' % url)

    # parse microformats, convert to ActivityStreams
    self.publish.html = self.fetched.text
    data = parser.Parser(doc=self.fetched.text, url=self.fetched.url).to_dict()
    logging.debug('Parsed microformats2: %s', data)
    items = data.get('items', [])
    if not items or not items[0]:
      return self.error('No microformats2 data found in %s' % self.fetched.url,
                        data=data)


    # loop through each item and try to preview/create it. if it fails, try the
    # next one. break after the first one that works.
    types = set()
    for item in items:
      try:
        resp = self.attempt_single_item(item)
        if resp:
          break
        else:
          # None return value means this item was valid but caused an error,
          # which has already been written to the response.
          return
      except NotImplementedError:
        # try the next item
        item_types = item.get('type')
        logging.error('Object type %s not supported; trying next.', item_types)
        types = types.union(item_types)
      except BaseException, e:
        return self.error('Error: %s' % e, status=500)
    else:
      if 'h-entry' in types:
        types.remove('h-entry')
      if types:
        msg = ("%s doesn't support type(s) %s." %
               (source_cls.AS_CLASS.NAME, ' + '.join(types)))
      else:
        msg = "Could not find h-entry or other content to publish!"
      return self.error(msg, data=data, log_exception=False)

    # write results to datastore
    self.publish.status = 'complete'
    self.publish.put()

    self.response.write(resp)
    # don't mail me about my own successful publishes, just the errors
    if domain != 'snarfed.org':
      self.mail_me(self.publish.published if self.PREVIEW else resp)

  def attempt_single_item(self, item):
    """Attempts to preview or publish a single mf2 item.

    Args:
      item: mf2 item dict from mf2py

    Returns: string HTTP response on success, otherwise None

    Raises:
      NotImplementedError if the source doesn't support this item type
    """
    obj = microformats2.json_to_object(item)
    # which original post URL to include? if the source URL redirected, use the
    # (pre-redirect) source URL, since it might be a short URL. otherwise, use
    # u-url if it's set. finally, fall back to the actual fetched URL
    if self.source_url != self.fetched.url:
      obj['url'] = self.source_url
    elif 'url' not in obj:
      obj['url'] = self.fetched.url
    logging.debug('Converted to ActivityStreams object: %s', obj)

    # posts and comments need content
    obj_type = obj.get('objectType')
    if obj_type in ('note', 'article', 'comment'):
      contents = item.get('properties', {}).get('content', [])
      if not contents or not contents[0] or not contents[0].get('value'):
        self.error('Could not find e-content in %s' % self.fetched.url, data=item)
        return None

    # special case for me: don't allow posts, just comments, likes, and reposts
    verb = obj.get('verb', '')
    if (not self.PREVIEW and self.source.domain == 'snarfed.org' and
        obj_type in ('note', 'article') and verb not in ('like', 'share') and
        not verb.startswith('rsvp-')):
      self.error('Not posting for snarfed.org')
      return None

    if self.PREVIEW:
      self.publish.published = self.source.as_source.preview_create(
        obj, include_link=True)
      return template.render('templates/preview.html', {
          'source': self.preprocess_source(self.source),
          'preview': self.publish.published,
          'source_url': self.fetched.url,
          'target_url': self.target_url,
          'webmention_endpoint': self.request.host_url + '/publish/webmention',
          })
    else:
      self.publish.published = self.source.as_source.create(obj, include_link=True)
      if 'url' not in self.publish.published:
        self.publish.published['url'] = obj.get('url')
      self.publish.type = self.publish.published.get('type') or models.get_type(obj)
      self.publish.type_label = self.source.TYPE_LABELS.get(self.publish.type)

      self.response.headers['Content-Type'] = 'application/json'
      return json.dumps(self.publish.published, indent=2)

  def mail_me(self, resp):
    subject = 'Bridgy publish %s %s' % (
      'preview' if self.PREVIEW else '',
      'complete' if self.publish and self.publish.status == 'complete' else 'failed')
    body = 'Request:\n%s\n\nResponse:\n%s' % (self.request.params.items(), resp)

    if self.source:
      body = 'Source: %s\n\n%s' % (self.source.bridgy_url(self), body)
      subject += ': %s' % self.source.label()

    util.email_me(subject=subject, body=body)

  @ndb.transactional
  def get_or_add_publish_entity(self, source_url):
    """Creates and stores Publish and (if necessary) PublishedPage entities.

    Args:
      source_url: string
    """
    page = PublishedPage.get_or_insert(source_url)
    entity = Publish.query(
      Publish.status == 'complete', Publish.type != 'preview',
      ancestor=page.key).get()

    if entity is None:
      entity = Publish(parent=page.key, source=self.source.key)
      if self.PREVIEW:
        entity.type = 'preview'
      entity.put()

    logging.debug('Publish entity: %s', entity.key.urlsafe())
    return entity


class PreviewHandler(Handler):
  """Renders a preview HTML snippet of how a webmention would be handled.
  """
  PREVIEW = True

  def error(self, error, status=400, data=None, log_exception=True):
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)
    self.response.set_status(status)
    if self.PREVIEW:
      error = util.linkify(error)
    self.response.write(error)
    self.mail_me(error)


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
    self.mail_me(resp)
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
<body>Nothing here! <a href="/about#publish">Try the docs instead.</a></body>
<html>""" % self.request.host_url)


application = webapp2.WSGIApplication([
    ('/publish/webmention', WebmentionHandler),
    ('/publish/preview', PreviewHandler),
    ('/publish/(facebook|twitter)', WebmentionLinkHandler),
    ],
  debug=appengine_config.DEBUG)
