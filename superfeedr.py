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
  for item in json.loads(feed).get('items', []):
    source.preprocess_superfeedr_item(item)
    # TODO: extract_links currently has a bug that makes it drop trailing
    # slashes. ugh. fix that.
    links = util.extract_links(item.get('content') or item.get('summary', ''))
    logging.info('Found links: %s', links)
    url = item.get('permalinkUrl') or item.get('id')
    if not url:
      logging.error('Dropping feed item without permalinkUrl or id!')
      continue
    models.BlogPost(id=url,
                    source=source.key,
                    feed_item=item,
                    unsent=links,
                    ).get_or_save()
