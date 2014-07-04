"""Superfeedr.

https://superfeedr.com/users/snarfed
http://documentation.superfeedr.com/subscribers.html
http://documentation.superfeedr.com/schema.html

If/when I add support for arbitrary RSS/Atom feeds, I should use
http://feediscovery.appspot.com/ for feed discovery based on front page URL.
"""

import json
import logging

import appengine_config
from appengine_config import HTTP_TIMEOUT

import models
import requests
from requests.auth import HTTPBasicAuth
import util
import webapp2

PUSH_API_URL = 'https://push.superfeedr.com'


def subscribe(source, handler):
  """Subscribes to a source.

  Also receives some past posts and adds propagate tasks for them.

  http://documentation.superfeedr.com/subscribers.html#addingfeedswithpubsubhubbub

  Args:
    source: Blogger, Tumblr, or WordPress
    handler: webapp2.RequestHandler
  """
  if appengine_config.DEBUG:
    logging.info('Running in dev_appserver, not subscribing to Superfeedr')
    return

  data = {
    'hub.mode': 'subscribe',
    'hub.topic': source.feed_url(),
    'hub.callback': '%s/%s/notify/%s' % (
      handler.request.host_url, source.SHORT_NAME, source.key.id()),
    # TODO
    # 'hub.secret': 'xxx',
    'format': 'json',
    'retrieve': 'true',
    }

  logging.info('Adding Superfeedr subscription: %s', data)
  resp = requests.post(PUSH_API_URL, data=data,
                       auth=HTTPBasicAuth(appengine_config.SUPERFEEDR_USERNAME,
                                          appengine_config.SUPERFEEDR_TOKEN),
                       timeout=HTTP_TIMEOUT)
  resp.raise_for_status()
  handle_feed(resp.text, source)


def handle_feed(feed, source):
  """Handles a Superfeedr JSON feed.

  Creates BlogPost entities and adds propagate-blogpost tasks for new items.

  http://documentation.superfeedr.com/schema.html#json
  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications

  Args:
    feed: string, Superfeedr JSON feed
    source: Blogger, Tumblr, or WordPress
  """
  logging.info('Source: %s %s', source.label(), source.key.string_id())
  logging.info('Raw feed: %s', feed)

  if source.status != 'enabled':
    logging.warning('Dropping because source is %s', source.status)
    return
  elif 'webmention' not in source.features:
    logging.warning("Dropping because source doesn't have webmention feature")
    return

  for item in json.loads(feed).get('items', []):
    url = item.get('permalinkUrl') or item.get('id')
    if not url:
      logging.error('Dropping feed item without permalinkUrl or id!')
      continue

    source.preprocess_superfeedr_item(item)
    # extract links from content, discarding self links.
    # TODO: extract_links currently has a bug that makes it drop trailing
    # slashes. ugh. fix that.
    content = item.get('content') or item.get('summary', '')
    links = [l for l in util.extract_links(content)
             if util.domain_from_link(l) not in source.domains]

    logging.info('Found links: %s', links)
    models.BlogPost(id=url,
                    source=source.key,
                    feed_item=item,
                    unsent=links,
                    ).get_or_save()


class NotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  Abstract; subclasses must set the SOURCE_CLS attr.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """
  SOURCE_CLS = None

  def post(self, id):
    source = self.SOURCE_CLS.get_by_id(id)
    if source:
      handle_feed(self.request.body, source)
