"""Superfeedr.

https://superfeedr.com/users/snarfed
http://documentation.superfeedr.com/subscribers.html
http://documentation.superfeedr.com/schema.html

If/when I add support for arbitrary RSS/Atom feeds, I should use
http://feediscovery.appspot.com/ for feed discovery based on front page URL.
"""
import logging

from flask import request
from flask.views import View
from google.cloud.ndb.key import _MAX_KEYPART_BYTES
from google.cloud.ndb._datastore_types import _MAX_STRING_LENGTH
from oauth_dropins.webutil import appengine_info
from requests.auth import HTTPBasicAuth

import models
import util

SUPERFEEDR_TOKEN = util.read("superfeedr_token")
SUPERFEEDR_USERNAME = util.read("superfeedr_username")
PUSH_API_URL = "https://push.superfeedr.com"
MAX_BLOGPOST_LINKS = 10


def subscribe(source):
    """Subscribes to a source.

    Also receives some past posts and adds propagate tasks for them.

    http://documentation.superfeedr.com/subscribers.html#addingfeedswithpubsubhubbub

    Args:
      source: Blogger, Tumblr, or WordPress
    """
    if appengine_info.LOCAL:
        logging.info("Running in dev_appserver, not subscribing to Superfeedr")
        return

    data = {
        "hub.mode": "subscribe",
        "hub.topic": source.feed_url(),
        "hub.callback": util.host_url(f"/{source.SHORT_NAME}/notify/{source.key_id()}"),
        # TODO
        # 'hub.secret': 'xxx',
        "format": "json",
        "retrieve": "true",
    }

    logging.info("Adding Superfeedr subscription: %s", data)
    resp = util.requests_post(
        PUSH_API_URL,
        data=data,
        auth=HTTPBasicAuth(SUPERFEEDR_USERNAME, SUPERFEEDR_TOKEN),
        headers=util.REQUEST_HEADERS,
    )
    handle_feed(resp.json(), source)


def handle_feed(feed, source):
    """Handles a Superfeedr JSON feed.

    Creates :class:`models.BlogPost` entities and adds propagate-blogpost tasks
    for new items.

    http://documentation.superfeedr.com/schema.html#json
    http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications

    Args:
      feed: unicode string, Superfeedr JSON feed
      source: Blogger, Tumblr, or WordPress
    """
    logging.info("Source: %s %s", source.label(), source.key_id())
    logging.info("Raw feed: %s", feed)

    if not feed:
        return

    if source.status != "enabled":
        logging.info("Dropping because source is %s", source.status)
        return
    elif "webmention" not in source.features:
        logging.info("Dropping because source doesn't have webmention feature")
        return

    for item in feed.get("items", []):
        url = item.get("permalinkUrl") or item.get("id")
        if not url:
            logging.error("Dropping feed item without permalinkUrl or id!")
            continue

        # extract links from content, discarding self links.
        #
        # i don't use get_webmention_target[s]() here because they follows redirects
        # and fetch link contents, and this handler should be small and fast and try
        # to return a response to superfeedr successfully.
        #
        # TODO: extract_links currently has a bug that makes it drop trailing
        # slashes. ugh. fix that.
        content = item.get("content") or item.get("summary", "")
        links = [
            util.clean_url(util.unwrap_t_umblr_com(url))
            for url in util.extract_links(content)
            if util.domain_from_link(url) not in source.domains
        ]

        unique = []
        for link in util.dedupe_urls(links):
            if len(link) <= _MAX_STRING_LENGTH:
                unique.append(link)
            else:
                logging.info(
                    "Giving up on link over %s chars! %s", _MAX_STRING_LENGTH, link
                )
            if len(unique) >= MAX_BLOGPOST_LINKS:
                logging.info("Stopping at 10 links! Skipping the rest.")
                break

        logging.info("Found links: %s", unique)
        if len(url) > _MAX_KEYPART_BYTES:
            logging.warning("Blog post URL is too long (over 500 chars)! Giving up.")
            bp = models.BlogPost(
                id=url[:_MAX_KEYPART_BYTES],
                source=source.key,
                feed_item=item,
                failed=unique,
            )
        else:
            bp = models.BlogPost(
                id=url, source=source.key, feed_item=item, unsent=unique
            )

        bp.get_or_save()


class Notify(View):
    """Handles a Superfeedr notification.

    Abstract; subclasses must set the SOURCE_CLS attr.

    http://documentation.superfeedr.com/subscribers.html#pubsubhubbubnotifications
    """

    SOURCE_CLS = None

    def dispatch_request(self, id):
        source = self.SOURCE_CLS.get_by_id(id)
        if source:
            handle_feed(request.json, source)

        return ""
