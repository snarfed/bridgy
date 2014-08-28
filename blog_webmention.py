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



def first_value(props, name):
  return next(iter(props.get(name, [])), None)


class BlogWebmentionHandler(webmention.WebmentionHandler):
  """Handler for incoming webmentions against blog providers.
  """

  def post(self, source_short_name):
    logging.info('Params: %self', self.request.params.items())
    # strip fragments from source and target url
    self.source_url = urlparse.urldefrag(util.get_required_param(self, 'source'))[0]
    self.target_url = urlparse.urldefrag(util.get_required_param(self, 'target'))[0]

    # clean target url (strip utm_* query params)
    self.target_url = util.clean_webmention_url(self.target_url)

    # parse and validate target URL
    domain = util.domain_from_link(self.target_url)
    if not domain:
      return self.error(msg, 'Could not parse target URL %s' % self.target_url)

    # look up source by domain
    source_cls = SOURCES[source_short_name]
    domain = domain.lower()
    self.source = (source_cls.query()
                   .filter(source_cls.domains == domain)
                   .filter(source_cls.features == 'webmention')
                   .filter(source_cls.status == 'enabled')
                   .get())
    if not self.source:
      return self.error(
        'Could not find %s account for %s. Is it registered with Bridgy?' %
        (source_cls.AS_CLASS.NAME, domain),
        mail=False)
    elif not self.source.verified():
      return self.error(
        "Bridgy account for %s isn't fully set up. See %s for details." %
        (self.source.label(), self.source.bridgy_url(self)),
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
    author = first_value(props, 'author')
    if author:
      if isinstance(author, basestring):
        author_name = author
      else:
        author_props = author.get('properties', {})
        author_name = first_value(author_props, 'name')
        author_url = first_value(author_props, 'url')

    # if present, u-url overrides source url
    u_url = first_value(props, 'url')
    if u_url:
      self.entity.u_url = u_url

    # generate content
    content = props['content'][0]  # find_mention_item() guaranteed this is here
    text = (content.get('html') or content.get('value')).strip()
    text += ' <br /> <a href="%s">via %s</a>' % (
      self.entity.source_url(), util.domain_from_link(self.entity.source_url()))

    # write comment
    try:
      self.entity.published = self.source.create_comment(
        self.target_url, author_name, author_url, text)
    except Exception, e:
      code, body = util.interpret_http_exception(e)
      if code or body:
        return self.error('Error: %s %s; %s' % (code, e, body), status=code)
      else:
        raise

    # write results to datastore
    self.entity.status = 'complete'
    self.entity.put()
    self.response.write(json.dumps(self.entity.published))

  def find_mention_item(self, data):
    """Returns the mf2 item that mentions (or replies to, likes, etc) the target.

    May modify the data arg, e.g. may set or replace content.html or
    content.value.

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
        if not text or self.target_url not in text:
          continue
        type = 'post'
        url = first_value(props, 'url') or self.source_url
        name = first_value(props, 'name') or first_value(props, 'summary')
        text = content['html'] = ('mentioned this in %s.' %
                                  util.pretty_link(url, text=name))

      if type:
        # found the target!
        rsvp = first_value(props, 'rsvp')
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
                                }[self.entity.type]
        return item

    return None


application = webapp2.WSGIApplication([
    ('/webmention/(blogger|fake|tumblr|wordpress)', BlogWebmentionHandler),
    ],
  debug=appengine_config.DEBUG)
