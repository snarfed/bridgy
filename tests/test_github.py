"""Unit tests for github.py."""
import copy

import granary
import granary.tests.test_github as gr_test_github
import oauth_dropins
from webutil.testutil import requests_response
from webutil.util import json_dumps, json_loads

import github
from . import testutil
import util


class GitHubTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    user = copy.deepcopy(gr_test_github.USER_GRAPHQL)
    user['login'] = 'Snarfed'
    self.auth_entity = oauth_dropins.github.GitHubAuth(
      id='Snarfed', access_token_str='towkin',
      user_json=json_dumps(user))

    self.auth_entity.put()
    self.gh = github.GitHub.new(self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.gh.auth_entity.get())
    self.assertEqual('snarfed', self.gh.key.id())
    self.assertEqual('Snarfed', self.gh.label_name())
    self.assertEqual('Ryan Barrett', self.gh.name)
    self.assertEqual('https://github.com/Snarfed', self.gh.silo_url())
    self.assertEqual('https://avatars2.githubusercontent.com/u/778068?v=4',
                     self.gh.picture)
    self.assertEqual('tag:github.com,2013:MDQ6VXNlcjc3ODA2OA==', self.gh.user_tag_id())

  def test_delete_finish(self):
    self.gh.features = ['listen']
    self.gh.put()
    state = util.encode_oauth_state({
      'feature': 'listen',
      'operation': 'delete',
      'source': self.gh.key.urlsafe().decode(),
    })

    self.mock_post.side_effect = [
      requests_response('access_token=towkin'),
      requests_response({'data': {'viewer': json_loads(self.auth_entity.user_json)}}),
    ]

    resp = self.client.get(f'/github/delete/finish?code=kode&state={state}')
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/', resp.headers['Location'])
    self.assertEqual([], self.gh.key.get().features)

  def test_delete_finish_declined(self):
    self.gh.features = ['listen']
    self.gh.put()
    state = util.encode_oauth_state({
      'feature': 'listen',
      'operation': 'delete',
      'source': self.gh.key.urlsafe().decode(),
    })

    resp = self.client.get(
      f'/github/delete/finish?error=access_denied&state={state}')
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/github/snarfed', resp.headers['Location'])
    self.assertEqual(['listen'], self.gh.key.get().features)
