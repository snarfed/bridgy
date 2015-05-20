#!/usr/bin/env python
"""Facebook integration test against the live API, using a canned user.

https://github.com/snarfed/bridgy/issues/406

The canned user is https://www.facebook.com/100009447618341 . He has one post
with one like and two comments:
https://www.facebook.com/100009447618341/posts/1407573252900915

I'd ideally like to use a Test User, but their posts can't have comments or
likes. :(
https://developers.facebook.com/docs/apps/test-users
"""

import logging
import unittest
import urllib
import urlparse

import alltests
import appengine_config

from activitystreams.oauth_dropins import facebook as oauth_facebook
from bs4 import BeautifulSoup
import facebook
import tasks
import testutil
import util

TEST_USER_ID = '1407574399567467'


class FacebookTestLive(testutil.HandlerTest):

  def test_live(self):
    # sign up (use the form inputs in our actual HTML template)
    with open('templates/facebook_signup.html') as f:
      resp = self.submit_form(f.read())

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

    # submit the "choose user/page" form. the only choice is the test user.
    self.submit_form(resp.text)
    source = facebook.FacebookPage.get_by_id(TEST_USER_ID)
    self.assertEqual('enabled', source.status)
    self.assertEqual(['listen'], source.features)

    # poll
    task = self.taskqueue_stub.GetTasks('poll')[0]
    resp = tasks.application.get_response(
      task['url'], method='POST', body=urllib.urlencode(testutil.get_task_params(task)))
    self.assertEqual(200, resp.status_int)

  @staticmethod
  def submit_form(html):
    """Submits the first form on the page."""
    form = BeautifulSoup(html).form
    data = {input['name']: input['value'] for input in form.find_all('input')
            if input.get('name') and input.get('value')}
    return facebook.application.get_response(
      form['action'], method=form['method'].upper(), body=urllib.urlencode(data))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.DEBUG)
  unittest.main()
