"""Converts webmentions to comments on Blogger, Tumblr, and WP.com.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import sys
import urllib2
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams import microformats2
from blogger import Blogger
import models
from models import BlogWebmention
import requests
from tumblr import Tumblr
import util
import webapp2
import webmention
from wordpress_rest import WordPress

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template

SOURCES = {cls.SHORT_NAME: cls for cls in (Blogger, WordPress, Tumblr)}


class BlogWebmentionHandler(webmention.WebmentionHandler):
  """Handler for incoming webmentions against blog providers.
  """

  def post(self, source_short_name):
    logging.info('Params: %self', self.request.params.items())
    # strip fragments from source and target url
    self.source_url = urlparse.urldefrag(util.get_required_param(self, 'source'))[0]
    self.target_url = urlparse.urldefrag(util.get_required_param(self, 'target'))[0]

    # parse and validate target URL
    domain = util.domain_from_link(self.target_url)
    if not domain:
      return self.error(msg, 'Could not parse target URL %s' % self.target_url)

    # look up source by domain
    source_cls = SOURCES[source_short_name]
    domain = domain.lower()
    self.source = (source_cls.query()
                   .filter(source_cls.domain == domain)
                   .filter(source_cls.features == 'webmention')
                   .get())
    if not self.source:
      return self.error(
        'Could not find %s account for %s. Is it registered with Bridgy?' %
        (source_cls.AS_CLASS.NAME, domain),
        mail=False)

    # create BlogWebmention entity
    id = '%s %s' % (self.source_url, self.target_url)
    self.entity = BlogWebmention.get_or_insert(id, source=self.source.key)
    if self.entity.status == 'complete':
      # TODO: response message saying update isn't supported
      self.response.write(self.entity.published)
      return
    logging.debug('BlogWebmention entity: %s', self.entity.key.urlsafe())

    # fetch source page
    resp = self.fetch_mf2(self.source_url)
    if not resp:
      return
    self.fetched, data = resp

    item = self.find_mention_item(data)
    if not item:
      return self.error('Could not find target URL %s in source page %s' %
                        (self.target_url, self.fetched.url),
                        data=data, log_exception=False)

    # default author to target domain
    author_name = domain
    author_url = 'http://%s/' % domain

    # extract author name and URL from h-card, if any
    props = item['properties']
    author = next(iter(props.get('author', [])), None)
    if author:
      if isinstance(author, basestring):
        author_name = author
      else:
        author_props = author.get('properties', {})
        author_name = next(iter(author_props.get('name', [])), None)
        author_url = next(iter(author_props.get('url', [])), None)

    # if present, u-url overrides source url
    u_url = next(iter(props.get('url', [])), None)
    if u_url:
      self.entity.u_url = u_url

    # generate content
    content = props['content'][0]  # find_mention_item() guaranteed this is here
    text = (content.get('html') or content.get('value')).strip()
    text += '<br /><a href="%s">via %s</a>' % (
      self.entity.source_url(), util.domain_from_link(self.entity.source_url()))

    # write comment
    try:
      self.entity.published = self.source.create_comment(
        self.target_url, author_name, author_url, text)
    except urllib2.HTTPError, e:
      body = e.read()
      logging.error('Error response body: %r', body)
      return self.error('Error: %s; %s' % (e, body), status=e.code)
    except requests.HTTPError, e:
      logging.error('Error response body: %r', e.response.text)
      return self.error('Error: %s; %s' % (e, e.response.text),
                        status=e.response.status_code)
    except BaseException, e:
      return self.error('Error: %s' % e, status=500)

    # write results to datastore
    self.entity.status = 'complete'
    self.entity.put()
    self.response.write(json.dumps(self.entity.published))

  def find_mention_item(self, data):
    """Returns the mf2 item that mentions (or replies to, likes, etc) the target.

    Args:
      data mf2 data dict

    Returns: mf2 item dict or None
    """
    # find target URL in source
    for item in data.get('items', []):
      props = item.setdefault('properties', {})

      # find first non-empty content element
      content = props.setdefault('content', [{}])[0]
      text = content.get('html') or content.get('value')

      for type in 'in-reply-to', 'like', 'like-of', 'repost', 'repost-of':
        urls = [urlparse.urldefrag(u)[0] for u in
                microformats2.get_string_urls(props.get(type, []))]
        if self.target_url in urls:
          break
      else:
        type = 'post' if text and self.target_url in text else None

      if type:
        # found the target!
        rsvp = next(iter(props.get('rsvp', [])), None)
        if rsvp:
          self.entity.type = 'rsvp'
          if not text:
            content['value'] = 'RSVPed %s.' % rsvp
        else:
          self.entity.type = {'in-reply-to': 'comment',
                              'like-of': 'like',
                              'repost-of': 'repost',
                              }.get(type, type)
          if not text:
            content['value'] = {'comment': 'replied to this.',
                                'like': 'liked this.',
                                'repost': 'reposted this.',
                                'post': 'mentioned this.'
                                }[self.entity.type]
        return item

    return None


application = webapp2.WSGIApplication([
    ('/webmention/(blogger|fake|tumblr|wordpress)', BlogWebmentionHandler),
    ],
  debug=appengine_config.DEBUG)
