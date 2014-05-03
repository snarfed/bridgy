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

from blogger import Blogger
import models
import requests
from requests.auth import HTTPBasicAuth
import util
from tumblr import Tumblr
import webapp2
from wordpress_rest import WordPress

SOURCES = {cls.SHORT_NAME: cls for cls in (Blogger, WordPress, Tumblr)}
PUSH_API_URL = 'https://push.superfeedr.com'


def subscribe(source, handler):
  """Subscribes to a source.

  Also receives some past posts and adds propagate tasks for them.

  http://documentation.superfeedr.com/subscribers.html#addingfeedswithpubsubhubbub

  Args:
    source: Blogger, Tumblr, or WordPress
  """
  data = {
    'hub.mode': 'subscribe',
    'hub.topic': source.feed_url(),
    'hub.callback': '%s/superfeedr/notify/%s/%s' % (
      handler.request.host_url, source.SHORT_NAME, source.key.id()),
    # TODO
    'hub.secret': 'xxx',
    # TODO?
    # 'hub.verify': 'sync',
    'format': 'json',
    'retrieve': 'true',
    }

  logging.info('Adding Superfeedr subscription: %s', data)
  resp = requests.post(PUSH_API_URL, data=data,
                       auth=HTTPBasicAuth(appengine_config.SUPERFEEDR_USERNAME,
                                          appengine_config.SUPERFEEDR_TOKEN),
                       timeout=HTTP_TIMEOUT)
  resp.raise_for_status()
  handle_feed(resp.json(), source)


def handle_feed(feed, source):
  """Handles a Superfeedr JSON feed.

  Creates BlogPost entities and adds propagate_blogpost tasks for new items.

  http://documentation.superfeedr.com/schema.html#json

  Args:
    feed: SuperFeeder JSON feed object
    source: Blogger, Tumblr, or WordPress
  """
  for item in feed.get('items', []):
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


class NotifyHandler(webapp2.RequestHandler):
  """Handles a Superfeedr notification.

  http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
  """

  def post(self, shortname, key_id):
    logging.info('Params: %s', self.request.params)
    logging.info('Body: %s', self.request.body)
    source = SOURCES[shortname].get_by_id(key_id)
    handle_feed(json.loads(self.request.body))


application = webapp2.WSGIApplication([
    ('/superfeedr/notify/(blogger|tumblr|wordpress)/(.+)', NotifyHandler),
    ], debug=appengine_config.DEBUG)
