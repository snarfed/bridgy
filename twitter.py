"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import email.utils
import json
import logging
import os
import re
import urllib
import urlparse

from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
import models
import tasks
import tweepy
import util

from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
import webapp2


class Twitter(models.Source):
  """A Twitter account.

  The key name is the username.
  """

  TYPE_NAME = 'Twitter'

  # full human-readable name
  name = db.StringProperty()
  picture = db.LinkProperty()
  auth_entity = db.ReferenceProperty(oauth_twitter.TwitterAuth)

  def display_name(self):
    return self.name

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a Twitter based on POST args.

    Args:
      handler: the current RequestHandler
    """
    user = json.loads(auth_entity.user_json)
    return Twitter(key_name=user['screen_name'],
                   owner=models.User.get_current_user(),
                   auth_entity=auth_entity,
                   url=Twitter.user_url(user['screen_name']),
                   name=user['name'],
                   picture=user['profile_image_url'])

  def get_posts(self):
    """Returns list of (JSON tweet, link url).

    The link url is also added to each returned JSON tweet in the 'bridgy_link'
    JSON value.

    https://developers.google.com/+/api/latest/activies#resource
    """
    # find tweets with links that include our base url.
    # search response is JSON tweets:
    # https://dev.twitter.com/docs/api/1.1/search/tweets
    results = self.search('%s filter:links' % util.domain_from_link(self.url))

    tweets_and_urls = []
    for result in results:
      # extract target url from tweet entities
      # https://dev.twitter.com/docs/tweet-entities
      target_url = None
      tweet_url = self.tweet_url(result['user'], result['id'])
      for url in result.get('entities', {}).get('urls', []):
        # expanded_url isn't always provided
        expanded_url = url.get('expanded_url', url['url'])

        if not expanded_url.startswith(self.url):
          # may be a shortened link. try following redirects.
          # (could use a service like http://unshort.me/api.html instead,
          # but not sure it'd buy us anything.)
          try:
            resolved = urlfetch.fetch(expanded_url, method='HEAD',
                                      follow_redirects=True, deadline=999)
            if getattr(resolved, 'final_url', None):
              logging.debug('Resolved short url %s to %s', expanded_url,
                            resolved.final_url)
              expanded_url = resolved.final_url
          except urlfetch.DownloadError, e:
            logging.error("Couldn't resolve URL: %s", e)

        if expanded_url.startswith(self.url):
          target_url = expanded_url

      if target_url:
        # logging.debug('Found post %s in tweet %s', target_url, tweet_url)
        result['bridgy_link'] = target_url
        tweets_and_urls.append((result, target_url))
      else:
        # logging.debug("Tweet %s should have %s link but doesn't. Maybe shortened?",
        #               tweet_url, self.url)
        pass

    return tweets_and_urls

  def get_comments(self, tweets_and_urls):
    # maps tweet id to TwitterReply
    replies = {}
    # maps username to list of @ mention search results, which includes replies
    mentions = {}

    # find and convert replies
    for tweet, url in tweets_and_urls:
      author = tweet['user'].get('screen_name')
      if not author:
        continue
      elif tweet['id'] in replies:
        logging.error('Already seen tweet %s! Should be impossible!', tweet['id'])
        continue

      reply = self.tweet_to_reply(tweet, url)
      # logging.debug('Found matching tweet %s', reply.source_post_url)
      replies[tweet['id']] = reply

      # get mentions of this tweet's author so we can search them for replies to
      # this tweet. can't use statuses/mentions_timeline because i'd need to
      # auth as the user being mentioned.
      # https://dev.twitter.com/docs/api/1.1/get/statuses/mentions_timeline
      if author not in mentions:
        mentions[author] = self.search('@%s' % author)

      # look for replies. add any we find to the end of tweets_and_urls.
      # this makes us recursively follow reply chains to their end. (python
      # supports appending to a sequence while you're iterating over it.)
      for mention in mentions[author]:
        if mention.get('in_reply_to_status_id') == tweet['id']:
          mention['bridgy_link'] = tweet['bridgy_link']
          tweets_and_urls.append((mention, url))

    return replies.values()

  @staticmethod
  def tweet_url(user, id):
    """Returns the URL of a tweet.
    """
    return 'http://twitter.com/%s/status/%d' % (user['screen_name'], id)

  @staticmethod
  def user_url(username):
    """Returns a user's URL.
    """
    return 'http://twitter.com/%s' % username


class TwitterReply(models.Comment):
  """Key name is the tweet (aka status) id.
  """

  # user who wrote the comment
  username = db.StringProperty(required=True)


class AddTwitter(oauth_twitter.CallbackHandler):
  messages = []

  def finish(self, auth_entity, state=None):
    Twitter.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


class DeleteTwitter(util.Handler):
  def post(self):
    twitter = Twitter.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (twitter.type_display_name(),
                                     twitter.display_name())
    twitter.delete()
    self.redirect('/?msg=' + msg)


application = webapp2.WSGIApplication([
    ('/twitter/start', oauth_twitter.StartHandler.to('/twitter/add')),
    ('/twitter/add', AddTwitter),
    ('/twitter/delete', DeleteTwitter),
    ], debug=appengine_config.DEBUG)
