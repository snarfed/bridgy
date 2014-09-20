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

import collections
import logging
import json
import mf2py
import pprint
import sys
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams import microformats2
from activitystreams import source as as_source
from activitystreams.oauth_dropins import handlers
from facebook import FacebookPage
from googleplus import GooglePlusPage
import html2text
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

SOURCE_NAMES = {
  cls.SHORT_NAME: cls for cls in
  (FacebookPage, Twitter, Instagram, GooglePlusPage)}
SOURCE_DOMAINS = {
  cls.AS_CLASS.DOMAIN: cls for cls in
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
    source_cls = SOURCE_NAMES.get(path_parts[-1])
    if (domain not in ('brid.gy', 'www.brid.gy', 'localhost:8080') or
        len(path_parts) != 2 or path_parts[0] != '/publish' or not source_cls):
      return self.error('Target must be brid.gy/publish/{facebook,twitter}')
    elif source_cls in (Instagram, GooglePlusPage):
      return self.error('Sorry, %s is not yet supported.' %
                        source_cls.AS_CLASS.NAME, mail=False)

    # resolve source URL
    url, domain, ok = util.get_webmention_target(self.source_url)
    # show nice error message if they're trying to publish a silo post
    if domain in SOURCE_DOMAINS:
      return self.error(
        "Looks like that's a %s URL. Try one from your web site instead!" %
        SOURCE_DOMAINS[domain].AS_CLASS.NAME)
    elif not ok:
      return self.error('Unsupported source URL %s' % url)
    elif not domain:
      return self.error('Could not parse source URL %s' % url)

    # When debugging locally, use snarfed.org for localhost webmentions
    if appengine_config.DEBUG and domain == 'localhost':
      domain = 'snarfed.org'

    # look up source by domain
    domain = domain.lower()
    sources = source_cls.query().filter(source_cls.domains == domain).fetch(100)
    if not sources:
      return self.error("Could not find <b>%(type)s</b> account for <b>%(domain)s</b>. Check that your %(type)s profile has %(domain)s in its <em>web site</em> or <em>link</em> field, then try signing up again." %
        {'type': source_cls.AS_CLASS.NAME, 'domain': domain})

    for source in sources:
      logging.info('Source: %s , features %s, status %s' %
                   (source.bridgy_url(self), source.features, source.status))
      if source.status == 'enabled' and 'publish' in source.features:
        self.source = source
        break
    else:
      return self.error(
        'Publish is not enabled for your account(s). Please visit %s and sign up!' %
        ' or '.join(s.bridgy_url(self) for s in sources))

    # show nice error message if they're trying to publish their home page
    for domain_url in self.source.domain_urls:
      domain_url_parts = urlparse.urlparse(domain_url)
      source_url_parts = urlparse.urlparse(self.source_url)
      if (source_url_parts.netloc == domain_url_parts.netloc and
          source_url_parts.path.strip('/') == domain_url_parts.path.strip('/') and
          not source_url_parts.query):
        return self.error(
          "Looks like that's your home page. Try one of your posts instead!",
          mail=False)

    # done with the sanity checks, ready to fetch the source url. create the
    # Publish entity so we can store the result.
    entity = self.get_or_add_publish_entity(url)
    if (entity.status == 'complete' and entity.type != 'preview' and
        not self.PREVIEW and not appengine_config.DEBUG):
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't yet support updating or deleting existing posts. Ping Ryan if you want that feature!",
                        mail=False)
    self.entity = entity

    # fetch source page
    resp = self.fetch_mf2(url)
    if not resp:
      return
    self.fetched, data = resp

    # loop through each item and its children and try to preview/create it. if
    # it fails, try the next one. break after the first one that works.
    resp = None
    types = set()
    queue = collections.deque(data.get('items', []))
    while queue:
      item = queue.popleft()
      item_types = set(item.get('type'))
      if 'h-feed' in item_types and 'h-entry' not in item_types:
        queue.extend(item.get('children', []))
        continue

      try:
        resp = self.attempt_single_item(item)
        if resp.content:
          break
        if resp.abort:
          return self.error(resp.error_plain, html=resp.error_html, data=item)
        # try the next item
        for embedded in ('rsvp', 'invitee', 'repost', 'repost-of', 'like',
                         'like-of', 'in-reply-to'):
          if embedded in item.get('properties', []):
            item_types.add(embedded)
        logging.error(
          'Object type(s) %s not supported; error=%s; trying next.',
          item_types, resp.error_plain)
        types = types.union(item_types)
        queue.extend(item.get('children', []))
      except BaseException, e:
        code, body = handlers.interpret_http_exception(e)
        return self.error('Error: %s %s' % (body or '', e), status=code or 500)

    if not resp.content:  # tried all the items
      types.discard('h-entry')
      types.discard('h-note')
      if types:
        msg = ("%s doesn't support type(s) %s, or no content was found.." %
               (source_cls.AS_CLASS.NAME, ' + '.join(types)))
      else:
        msg = 'Could not find <a href="http://microformats.org/">h-entry</a> or other content to publish!'
      return self.error(msg, data=data)

    # write results to datastore
    self.entity.status = 'complete'
    self.entity.put()
    self.response.write(resp.content)

  def attempt_single_item(self, item):
    """Attempts to preview or publish a single mf2 item.

    Args:
      item: mf2 item dict from mf2py

    Returns:
      a CreationResult object, where content is the string HTTP
      response or None if the source cannot publish this item type.
    """
    obj = microformats2.json_to_object(item)
    # which original post URL to include? if the source URL redirected, use the
    # (pre-redirect) source URL, since it might be a short URL. otherwise, use
    # u-url if it's set. finally, fall back to the actual fetched URL
    if self.source_url != self.fetched.url:
      obj['url'] = self.source_url
    elif 'url' not in obj:
      obj['url'] = self.fetched.url
    logging.debug('Converted to ActivityStreams object: %s', pprint.pformat(obj))

    # posts and comments need content
    props = item.get('properties', {})
    obj_type = obj.get('objectType')
    if obj_type in ('note', 'article', 'comment'):
      if (not obj.get('content') and not obj.get('summary') and
          not obj.get('displayName')):
        return as_source.creation_result(
          abort=False,
          error_plain='Could not find content in %s' % self.fetched.url,
          error_html='Could not find <a href="http://microformats.org/">content</a> in %s' % self.fetched.url)

    self.preprocess_activity(obj)

    # special case for me: don't allow posts in live app, just comments, likes,
    # and reposts
    verb = obj.get('verb', '')
    if (not appengine_config.DEBUG and 'snarfed.org' in self.source.domains and
        not self.PREVIEW and obj_type in ('note', 'article') and
        verb not in ('like', 'share') and not verb.startswith('rsvp-')):
      return as_source.creation_result(
        abort=True, error_plain='Not posting for snarfed.org',
        error_html='Not posting for snarfed.org')

    # whether to include link to original post. bridgy_omit_link query param
    # (any value) takes precedence, then u-bridgy-omit-link mf2 class.
    if 'bridgy_omit_link' in self.request.params:
      omit_link = self.request.get('bridgy_omit_link').lower() in ('', 'true')
    else:
      omit_link = 'bridgy-omit-link' in props

    if self.PREVIEW:
      result = self.source.as_source.preview_create(
        obj, include_link=not omit_link)
      self.entity.published = result.content
      if not result.content:
        return result  # there was an error
      vars = {'source': self.preprocess_source(self.source),
              'preview': self.entity.published,
              'description': result.description,
              'source_url': self.source_url,
              'target_url': self.target_url,
              'bridgy_omit_link': omit_link,
              'webmention_endpoint': self.request.host_url + '/publish/webmention',
              }
      logging.info('Rendering preview with template vars %s', pprint.pformat(vars))
      return as_source.creation_result(
        template.render('templates/preview.html', vars))

    else:
      result = self.source.as_source.create(obj, include_link=not omit_link)
      self.entity.published = result.content
      if not result.content:
        return result  # there was an error
      if 'url' not in self.entity.published:
        self.entity.published['url'] = obj.get('url')
      self.entity.type = self.entity.published.get('type') or models.get_type(obj)
      self.entity.type_label = self.source.TYPE_LABELS.get(self.entity.type)
      self.response.headers['Content-Type'] = 'application/json'
      logging.info('Returning %s', pprint.pformat(self.entity.published))
      return as_source.creation_result(
        json.dumps(self.entity.published, indent=2))

  def preprocess_activity(self, activity):
    """Preprocesses an item before trying to publish it.

    Specifically:
    * Expands inReplyTo/object URLs with rel=syndication URLs.
    * Renders the HTML content to text using html2text so that whitespace is
      formatted like in the browser.

    Args:
      activity: an ActivityStreams dict of the activity being published
    """
    self.expand_target_urls(activity)

    content = activity.get('content')
    if content:
      h = html2text.HTML2Text()
      h.unicode_snob = True
      h.body_width = 0  # don't wrap lines
      h.ignore_links = True
      h.ignore_images = True
      activity['content'] = '\n'.join(
        # strip trailing whitespace that html2text adds to ends of some lines
        line.rstrip() for line in h.handle(content).splitlines())
      logging.info('Rendered content to:\n%s', activity['content'])

  def expand_target_urls(self, activity):
    """Expand the inReplyTo or object fields of an ActivityStreams object
    by fetching the original and looking for rel=syndication URLs.

    This method modifies the dict in place.

    Args:
      activity: an ActivityStreams dict of the activity being published
    """
    for field in ('inReplyTo', 'object'):
      # microformats2.json_to_object de-dupes, no need to do it here
      objs = activity.get(field)
      if not objs:
        continue

      if isinstance(objs, dict):
        objs = [objs]

      augmented = list(objs)
      for obj in objs:
        url = obj.get('url')
        if not url:
          continue

        # get_webmention_target weeds out silos and non-HTML targets
        # that we wouldn't want to download and parse
        url, _, ok = util.get_webmention_target(url)
        if not ok:
          continue

        # fetch_mf2 raises a fuss if it can't fetch a mf2 document;
        # easier to just grab this ourselves than add a bunch of
        # special-cases to that method
        logging.debug('expand_target_urls fetching field=%s, url=%s', field, url)
        try:
          resp = requests.get(url, timeout=HTTP_TIMEOUT)
          resp.raise_for_status()
          data = mf2py.Parser(url=url, doc=resp.text).to_dict()
        except AssertionError:
          raise  # for unit tests
        except BaseException:
          # it's not a big deal if we can't fetch an in-reply-to url
          logging.warning('expand_target_urls could not fetch field=%s, url=%s',
                          field, url, exc_info=True)
          continue

        synd_urls = data.get('rels', {}).get('syndication', [])

        # look for syndication urls in the first h-entry
        queue = collections.deque(data.get('items', []))
        while queue:
          item = queue.popleft()
          item_types = set(item.get('type', []))
          if 'h-feed' in item_types and 'h-entry' not in item_types:
            queue.extend(item.get('children', []))
            continue

          # these can be urls or h-cites
          synd_urls += microformats2.get_string_urls(
            item.get('properties', {}).get('syndication', []))

        logging.debug('expand_target_urls found rel=syndication for url=%s: %r', url, synd_urls)
        augmented += [{'url': u} for u in synd_urls]

      activity[field] = augmented

  @ndb.transactional
  def get_or_add_publish_entity(self, source_url):
    """Creates and stores Publish and (if necessary) PublishedPage entities.

    Args:
      source_url: string
    """
    page = PublishedPage.get_or_insert(source_url)
    entity = Publish.query(
      Publish.status == 'complete', Publish.type != 'preview',
      Publish.source == self.source.key,
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

  def error(self, error, html=None, status=400, data=None, mail=True):
    logging.warning(error, exc_info=True)
    self.response.set_status(status)
    error = html if html else util.linkify(error)
    self.response.write(error)
    if mail:
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
