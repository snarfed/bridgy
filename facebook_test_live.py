#!/usr/bin/env python
"""Facebook integration test against the live API, using a test user.

https://github.com/snarfed/bridgy/issues/406
https://developers.facebook.com/docs/apps/test-users
"""

import unittest
import urllib
import urlparse

import alltests
import appengine_config

from activitystreams.oauth_dropins import facebook as oauth_facebook
from bs4 import BeautifulSoup
import facebook
import testutil
import util


class FacebookTestLive(testutil.HandlerTest):

  def test_live(self):
    # self.mox.stubs.UnsetAll()

    # sign up (use the form inputs in our actual HTML template)
    with open('templates/facebook_signup.html') as f:
      doc = BeautifulSoup(f.read())
      data = {input['name']: input['value'] for input in doc.find_all('input')
              if input.get('value')}

    resp = facebook.application.get_response('/facebook/start', method='POST',
                                             body=urllib.urlencode(data))
    self.assertEqual(302, resp.status_int)
    to = resp.headers['Location']
    self.assertTrue(to.startswith('https://www.facebook.com/v2.2/dialog/oauth?'), to)
    redirect = urlparse.parse_qs(urlparse.urlparse(to).query)['redirect_uri'][0]

    # pretend the user approves the prompt and facebook redirects back to us.
    # mock out the access token request since we use a canned token.
    self.expect_urlopen(oauth_facebook.GET_ACCESS_TOKEN_URL % {
        'client_id': appengine_config.FACEBOOK_APP_ID,
        'client_secret': appengine_config.FACEBOOK_APP_SECRET,
        'redirect_uri': urllib.quote_plus(redirect),
        'auth_code': 'fake_code',
      },
      'access_token=%s' % appengine_config.FACEBOOK_TEST_USER_TOKEN,
      ).WithSideEffects(lambda *args, **kwargs: self.mox.stubs.UnsetAll())
    self.mox.ReplayAll()

    resp = facebook.application.get_response(
      util.add_query_params(redirect, {'code': 'fake_code'}))
    self.assertEqual(200, resp.status_int)


if __name__ == '__main__':
  unittest.main()
