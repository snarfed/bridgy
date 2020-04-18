"""Unit tests for pixelfed.py."""
from oauth_dropins import pixelfed as oauth_pixelfed
from oauth_dropins.webutil.util import json_dumps, json_loads

from . import testutil
from pixelfed import Pixelfed


class PixelfedTest(testutil.ModelsTest):

  def setUp(self):
    super(PixelfedTest, self).setUp()

    app = oauth_pixelfed.PixelfedApp(instance='https://foo.com', data='')
    app.put()
    self.auth_entity = oauth_pixelfed.PixelfedAuth(
      id='@me@foo.com', access_token_str='towkin', app=app.key, user_json=json_dumps({
        'id': '123',
        'username': 'me',
        'acct': 'me',
        'url': 'https://foo.com/@me',
        'display_name': 'Ryan Barrett',
        'avatar': 'http://pi.ct/ure',
      }))
    self.auth_entity.put()
    self.p = Pixelfed.new(self.handler, auth_entity=self.auth_entity)
