"""Unit tests for tumblr.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox

from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins import tumblr as oauth_tumblr
from tumblr import Tumblr
import testutil


class TumblrTest(testutil.HandlerTest):

  def setUp(self):
    super(TumblrTest, self).setUp()
    self.auth_entity = oauth_tumblr.TumblrAuth(id='name', user_json=json.dumps({
          'user': {'blogs': [{'url': 'other'},
                             {'url': 'http://primary/', 'primary': True}]}}))

  def test_new(self):
    # based on http://snarfed.tumblr.com/
    self.expect_requests_get('http://primary/', """
<html><body>
some stuff
<script charset="utf-8" type="text/javascript" src="http://disqus.com/forums/my-disqus-name/get_num_replies.js?url131=...&amp;"></script>
</body></html>""")
    self.mox.ReplayAll()

    tumblr = Tumblr.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, tumblr.auth_entity)
    self.assertEquals('name', tumblr.name)
    self.assertEquals('http://primary/', tumblr.domain_url)
    self.assertEquals('primary', tumblr.domain)
    self.assertEquals('my-disqus-name', tumblr.disqus_shortname)

