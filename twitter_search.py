"""Twitter source code and datastore model classes.

Python code to pretty-print JSON responses from Twitter Search API:
pprint.pprint(json.loads(urllib.urlopen(
  'http://search.twitter.com/search.json?q=snarfed.org+filter%3Alinks&include_entities=true&result_type=recent&rpp=100').read()))
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

TWITTER_ACCESS_TOKEN_KEY = appengine_config.read('twitter_access_token_key')
TWITTER_ACCESS_TOKEN_SECRET = appengine_config.read('twitter_access_token_secret')


class TwitterSearch(models.Source):
  """A Twitter search.

  The key name is the base url to search for.
  """

  TYPE_NAME = 'Twitter'

  def __init__(self, *args, **kwargs):
    super(TwitterSearch, self).__init__(*args, **kwargs)
    if 'url' in kwargs:
      self.picture = util.favicon_for_url(kwargs['url'])

  @staticmethod
  def new(handler):
    """Creates and returns a TwitterSearch based on POST args.

    Args:
      handler: the current RequestHandler
    """
    url = handler.request.params['url']
    return TwitterSearch(key_name=url,
                         url=url,
                         owner=models.User.get_current_user())

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
      # extract destination post url from tweet entities
      # https://dev.twitter.com/docs/tweet-entities
      dest_post_url = None
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
          dest_post_url = expanded_url

      if dest_post_url:
        # logging.debug('Found post %s in tweet %s', dest_post_url, tweet_url)
        result['bridgy_link'] = dest_post_url
        tweets_and_urls.append((result, dest_post_url))
      else:
        # logging.debug("Tweet %s should have %s link but doesn't. Maybe shortened?",
        #               tweet_url, self.url)
        pass

    return tweets_and_urls

  def get_comments(self, tweets_and_dests):
    # maps tweet id to TwitterReply
    replies = {}
    # maps username to list of @ mention search results, which includes replies
    mentions = {}

    # find and convert replies
    for tweet, dest in tweets_and_dests:
      author = tweet['user'].get('screen_name')
      if not author:
        continue
      elif tweet['id'] in replies:
        logging.error('Already seen tweet %s! Should be impossible!', tweet['id'])
        continue

      reply = self.tweet_to_reply(tweet, dest)
      # logging.debug('Found matching tweet %s', reply.source_post_url)
      replies[tweet['id']] = reply

      # get mentions of this tweet's author so we can search them for replies to
      # this tweet. can't use statuses/mentions_timeline because i'd need to
      # auth as the user being mentioned.
      # https://dev.twitter.com/docs/api/1.1/get/statuses/mentions_timeline
      if author not in mentions:
        mentions[author] = self.search('@%s' % author)

      # look for replies. add any we find to the end of tweets_and_dests.
      # this makes us recursively follow reply chains to their end. (python
      # supports appending to a sequence while you're iterating over it.)
      for mention in mentions[author]:
        if mention.get('in_reply_to_status_id') == tweet['id']:
          mention['bridgy_link'] = tweet['bridgy_link']
          tweets_and_dests.append((mention, dest))

    return replies.values()

  def search(self, query):
    """Searches for tweets using the Twitter Search API.

    Background:
    https://dev.twitter.com/docs/using-search
    https://dev.twitter.com/docs/api/1/get/search
    http://stackoverflow.com/questions/2693553/replies-to-a-particular-tweet-twitter-api

    Args:
      query: string (not url-encoded)

    Returns: dict, JSON results
    """
    url_without_query = 'https://api.twitter.com/1.1/search/tweets.json'
    url = url_without_query + (
      '?q=%s&include_entities=true&result_type=recent&count=100' %
      urllib.quote_plus(query))
    parsed = urlparse.urlparse(url)
    headers = {}

    auth = tweepy.OAuthHandler(appengine_config.TWITTER_APP_KEY,
                               appengine_config.TWITTER_APP_SECRET)
    # make sure token key and secret aren't unicode because python's hmac
    # module (used by tweepy/oauth.py) expects strings.
    # http://stackoverflow.com/questions/11396789
    auth.set_access_token(str(TWITTER_ACCESS_TOKEN_KEY),
                          str(TWITTER_ACCESS_TOKEN_SECRET))
    auth.apply_auth(url_without_query, 'GET', headers,
                    dict(urlparse.parse_qsl(parsed.query)))

    logging.debug('Fetching %s', url)
    resp = urlfetch.fetch(url, headers=headers, deadline=999)
    resp_json = json.loads(resp.content)
    assert resp.status_code == 200, resp.content
    return resp_json['statuses']

  def tweet_to_reply(self, tweet, dest):
    """Converts a tweet JSON dict to a TwitterReply.
    """
    id = tweet['id']
    user = tweet['user']
    source_post_url = self.tweet_url(user, id)
    replier_name = (user['name'] if user['name'] else '@' + user['screen_name'])

    # parse the timestamp, format e.g. "Fri Sep 21 22:51:18 +0800 2012"
    timetuple = list(email.utils.parsedate_tz(tweet['created_at']))
    del timetuple[6:9]  # these are day of week, week of month, and is_dst
    created = datetime.datetime(*timetuple)
    return TwitterReply(
      key_name=str(id),
      source=self,
      dest=dest,
      source_post_url=source_post_url,
      dest_post_url=tweet['bridgy_link'],
      created=created,
      author_name=replier_name,
      author_url=self.user_url(user['screen_name']),
      content=self.linkify(tweet['text']),
      username=user['screen_name'],
      )

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

  @staticmethod
  def linkify(text):
    """Converts @mentions and hashtags to HTML links.
    """
    # twitter usernames can only have \w chars, ie letters, numbers, or
    # underscores. the pattern matches @, *not* preceded by a \w char, followed
    # one or more \w chars.
    text = re.sub(r'(?<!\w)@(\w+)',
                 r'<a href="http://twitter.com/\1">\g<0></a>',
                 text)

    # no explicit info about hashtag chars, but i assume the same.
    text = re.sub(r'(?<!\w)#(\w+)',
                 r'<a href="http://twitter.com/search?q=%23\1">\g<0></a>',
                 text)
    return text


class TwitterReply(models.Comment):
  """Key name is the tweet (aka status) id.
  """

  # user who wrote the comment
  username = db.StringProperty(required=True)


class AddTwitterSearch(util.Handler):
  def post(self):
    TwitterSearch.create_new(self)
    self.redirect('/')


class DeleteTwitterSearch(util.Handler):
  def post(self):
    search = TwitterSearch.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (search.type_display_name(),
                                     search.display_name())
    search.delete()
    self.redirect('/?msg=' + msg)


application = webapp2.WSGIApplication([
    ('/twitter/add_search', AddTwitterSearch),
    ('/twitter/delete_search', DeleteTwitterSearch),
    ], debug=appengine_config.DEBUG)
