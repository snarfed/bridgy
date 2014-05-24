"""Publishes webmentions into the silos.

Webmention spec: http://webmention.org/

Bridgy request and response details: http://www.brid.gy/about#response

Example request:

    POST /webmention HTTP/1.1
    Host: brid.gy
    Content-Type: application/x-www-url-form-encoded

    source=http://bob.host/post-by-bob&
    target=http://facebook.com/123

Example response:

    HTTP/1.1 200 OK

    {
      "url": "http://facebook.com/456_789",
      "type": "post",
      "id": "456_789"
    }
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import sys
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams import microformats2
from facebook import FacebookPage
from googleplus import GooglePlusPage
from instagram import Instagram
import models
from models import Publish, PublishedPage
import requests
from twitter import Twitter
import util
import webapp2
import webmention

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template

SOURCES = {cls.SHORT_NAME: cls for cls in
           (FacebookPage, Twitter, Instagram, GooglePlusPage)}


class Handler(webmention.WebmentionHandler):
  """Base handler for both previews and publishes.

  Subclasses must set the PREVIEW attribute to True or False.

  Attributes:
    source_url: string
    target_url: string
    fetched: requests.Response from fetching source_url
  """
  PREVIEW = None

  def post(self):
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
    domain = domain.lower()
    self.source = (source_cls.query()
                   .filter(source_cls.domain == domain)
                   .filter(source_cls.features == 'publish')
                   .get())
    if not self.source:
      return self.error("Could not find <b>%(type)s</b> account for <b>%(domain)s</b>. Check that your %(type)s profile has %(domain)s in its <em>web site</em> or <em>link</em> field, then try signing up again." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    # show nice error message if they're trying to publish their home page
    domain_url_parts = urlparse.urlparse(self.source.domain_url)
    source_url_parts = urlparse.urlparse(self.source_url)
    if (source_url_parts.netloc == domain_url_parts.netloc and
        source_url_parts.path.strip('/') == domain_url_parts.path.strip('/')):
      return self.error(
        "Looks like that's your home page. Try entering pone of your posts instead!")

    # done with the sanity checks, ready to fetch the source url. create the
    # Publish entity so we can store the result.
    entity = self.get_or_add_publish_entity(url)
    if (entity.status == 'complete' and entity.type != 'preview' and
        not self.PREVIEW and not appengine_config.DEBUG):
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't yet support updating or deleting existing posts. Ping Ryan if you want that feature!")
    self.entity = entity

    # fetch source page
    resp = self.fetch_mf2(url)
    if not resp:
      return
    self.fetched, data = resp

    # loop through each item and try to preview/create it. if it fails, try the
    # next one. break after the first one that works.
    types = set()
    for item in data.get('items', []):
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
        item_types = set(item.get('type'))
        for embedded in ('rsvp', 'invitee', 'repost', 'repost-of', 'like',
                         'like-of', 'in-reply-to'):
          if embedded in item.get('properties', []):
            item_types.add(embedded)
        logging.error('Object type(s) %s not supported; trying next.', item_types)
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
    self.entity.status = 'complete'
    self.entity.put()

    self.response.write(resp)
    # don't mail me about my own successful publishes, just the errors
    if domain != 'snarfed.org':
      self.mail_me(self.entity.published if self.PREVIEW else resp)

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
    props = item.get('properties', {})
    obj_type = obj.get('objectType')
    if obj_type in ('note', 'article', 'comment'):
      contents = props.get('content', [])
      if not contents or not contents[0] or not contents[0].get('value'):
        self.error('Could not find e-content in %s' % self.fetched.url, data=item)
        return None

    # special case for me: don't allow posts in live app, just comments, likes,
    # and reposts
    verb = obj.get('verb', '')
    if (not appengine_config.DEBUG and self.source.domain == 'snarfed.org' and
        not self.PREVIEW and obj_type in ('note', 'article') and
        verb not in ('like', 'share') and not verb.startswith('rsvp-')):
      self.error('Not posting for snarfed.org')
      return None

    # whether to include link to original post. bridgy_omit_link query param
    # (any value) takes precedence, then u-bridgy-omit-link mf2 class.
    if 'bridgy_omit_link' in self.request.params:
      omit_link = self.request.get('bridgy_omit_link').lower() in ('', 'true')
    else:
      omit_link = 'bridgy-omit-link' in props

    if self.PREVIEW:
      self.entity.published = self.source.as_source.preview_create(
        obj, include_link=not omit_link)
      return template.render('templates/preview.html', {
          'source': self.preprocess_source(self.source),
          'preview': self.entity.published,
          'source_url': self.fetched.url,
          'target_url': self.target_url,
          'bridgy_omit_link': omit_link,
          'webmention_endpoint': self.request.host_url + '/publish/webmention',
          })
    else:
      self.entity.published = self.source.as_source.create(
        obj, include_link=not omit_link)
      if 'url' not in self.entity.published:
        self.entity.published['url'] = obj.get('url')
      self.entity.type = self.entity.published.get('type') or models.get_type(obj)
      self.entity.type_label = self.source.TYPE_LABELS.get(self.entity.type)

      self.response.headers['Content-Type'] = 'application/json'
      return json.dumps(self.entity.published, indent=2)

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

  def error(self, error, html=None, status=400, data=None, log_exception=True):
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)
    self.response.set_status(status)
    error = util.linkify(html if html else error)
    self.response.write(error)
    self.mail_me(error)


class PublishHandler(Handler):
  """Accepts webmentions and translates them to site-specific API calls.
  """
  PREVIEW = False


application = webapp2.WSGIApplication([
    ('/publish/webmention', PublishHandler),
    ('/publish/preview', PreviewHandler),
    ('/publish/(facebook|twitter)', webmention.WebmentionGetHandler),
    ],
  debug=appengine_config.DEBUG)
