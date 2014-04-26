"""Converts webmentions to comments on Blogger, Tumblr, and WP.com.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import sys
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

import models
from models import BlogWebmention
import requests
import util
import webapp2
import webmention
from wordpress_rest import WordPress

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template

SOURCES = {cls.SHORT_NAME: cls for cls in
           (WordPress, # ...
            )}


class Handler(webmention.WebmentionHandler):
  """Base handler for both previews and publishes.

  Subclasses must set the PREVIEW attribute to True or False.
  """

  def post(self, source_short_name):
    logging.info('Params: %self', self.request.params.items())
    self.source_url = util.get_required_param(self, 'source')
    self.target_url = util.get_required_param(self, 'target')

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
        (source_cls.AS_CLASS.NAME, domain))

    # fetch source page
    resp = self.fetch_mf2(self.source_url)
    if not resp:
      return
    self.fetched, data = resp

    # TODO
    # self.entity = self.get_or_add_publish_entity(url)

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

    content = props['content'][0]  # find_mention_item() guaranteed this is here
    text = (content.get('html') or content.get('value')).strip()
    self.source.create_comment(self.target_url, author_name, author_url, text)

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

      for type in 'in-reply-to', 'like-of', 'repost-of':
        if self.target_url in props.get(type, []):
          # found the target!
          if not text:
            text = content['value'] = {'in-reply-to': 'replied to this.',
                                       'like-of': 'liked this.',
                                       'repost-of': 'reposted this.',
                                       'mention': 'mentioned this.',
                                       }[type]
          return item

      if text and self.target_url in text:
        return item  # a normal mention

    return None


application = webapp2.WSGIApplication([
    ('/webmention/(fake|wordpress)', Handler),
    ],
  debug=appengine_config.DEBUG)
