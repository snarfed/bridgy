"""Unit tests for github.py."""
import json

import appengine_config
import github
import granary
import granary.test.test_github
import oauth_dropins
import testutil


class GitHubTest(testutil.ModelsTest):

  def setUp(self):
    super(GitHubTest, self).setUp()
    self.auth_entity = oauth_dropins.github.GitHubAuth(
      id='snarfed', access_token_str='towkin',
      user_json=json.dumps(granary.test.test_github.USER_GRAPHQL))

    self.auth_entity.put()
    self.gh = github.GitHub.new(self.handler, self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.gh.auth_entity.get())
    self.assertEqual('snarfed', self.gh.key.id())
    self.assertEqual('snarfed', self.gh.label_name())
    self.assertEqual('Ryan Barrett', self.gh.name)
    self.assertEqual('https://github.com/snarfed', self.gh.silo_url())
    self.assertEqual('https://avatars2.githubusercontent.com/u/778068?v=4',
                     self.gh.picture)
