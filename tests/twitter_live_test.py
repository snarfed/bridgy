#!/usr/bin/env python
"""Twitter integration test against the live site.

Just checks that scraping likes with a logged in session still works and the
session hasn't expired.

https://github.com/snarfed/bridgy/issues/949
"""
import logging
import os
import sys
import unittest

from oauth_dropins.webutil import util
from oauth_dropins import twitter_auth
from granary import twitter

from models import TWITTER_SCRAPE_HEADERS

twitter_auth.TWITTER_APP_KEY = os.getenv('TWITTER_LIVE_TEST_APP_KEY')
twitter_auth.TWITTER_APP_SECRET = os.getenv('TWITTER_LIVE_TEST_APP_SECRET')

TOKEN_KEY = (os.getenv('TWITTER_ACCESS_TOKEN_KEY') or
             util.read('twitter_access_token_key'))
TOKEN_SECRET = (os.getenv('TWITTER_ACCESS_TOKEN_SECRET') or
                util.read('twitter_access_token_secret'))
TWEET_ID = '1270018109630369797'


class TwitterLiveTest(unittest.TestCase):

  def test_like_scraping(self):
    tw = twitter.Twitter(TOKEN_KEY, TOKEN_SECRET,
                         scrape_headers=TWITTER_SCRAPE_HEADERS)
    activities = tw.get_activities(activity_id=TWEET_ID, fetch_likes=True)
    likes = [t for t in activities[0]['object']['tags'] if t.get('verb') == 'like']
    self.assertGreater(len(likes), 0)


if __name__ == '__main__':
  if '--debug' in sys.argv:
    sys.argv.remove('--debug')
    logging.getLogger().setLevel(logging.DEBUG)
  else:
    logging.getLogger().setLevel(logging.CRITICAL + 1)
  unittest.main()
