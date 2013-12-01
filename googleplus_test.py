"""Unit tests for googleplus.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import copy
import datetime
import mox
import testutil

from apiclient.errors import HttpError
from oauth2client.appengine import CredentialsModel
from oauth2client.client import AccessTokenCredentials

from activitystreams.oauth_dropins import googleplus as oauth_googleplus
import googleplus
from googleplus import GooglePlusPage
import models


class GooglePlusPageTest(testutil.ModelsTest):

  def setUp(self):
    super(GooglePlusPageTest, self).setUp()
    self.handler.messages = []

    self.auth_entity = oauth_googleplus.GooglePlusAuth(
      key_name='x', creds_json='x', user_json='x')
    self.page = GooglePlusPage(key_name='2468',
                               auth_entity=self.auth_entity,
                               name='my full name',
                               url='http://my.g+/url',
                               picture='http://my.pic/small',
                               type='user')

    self.people_get_response = {
        'id': '2468',
        'displayName': 'my full name',
        'url': 'http://my.g+/url',
        'image': {'url': 'http://my.pic/small'},
        'type': 'person',
        }

    self.activities = [
        # no attachments
        {'object': {}},
        # no article attachment
        {'object': {'attachments': [{'objectType': 'note'}]}},
        # matches self.targets[1]
        {'object': {'attachments': [{'objectType': 'article',
                                     'url': 'http://target1/post/url'}]},
         'id': '1',
         'url': 'http://source/post/1',
         },
        # matches self.targets[0]
        {'object': {'attachments': [{'objectType': 'article',
                                     'url': 'http://target0/post/url'}]},
         'id': '2',
         'url': 'http://source/post/0',
         },
        ]
    self.activities_list_response = {'items': copy.deepcopy(self.activities)}

    self.activities_with_urls = []
    for i, link in ((3, 'http://target1/post/url'),
                    (4, 'http://target0/post/url')):
      self.activities[i]['bridgy_link'] = link
      self.activities_with_urls.append((self.activities[i], link))

    # TODO: unify with ModelsTest.setUp()
    self.comments = [
      GooglePlusComment(
        key_name='123',
        created=datetime.datetime.utcfromtimestamp(1.01),
        source=self.page,
        source_post_url='http://source/post/1',
        target_url='http://target1/post/url',
        author_name='fred',
        author_url='http://fred',
        content='foo',
        user_id='4',
        ),
      GooglePlusComment(
        key_name='789',
        created=datetime.datetime.utcfromtimestamp(2.01),
        source=self.page,
        source_post_url='http://source/post/0',
        target_url='http://target0/post/url',
        author_name='bob',
        author_url='http://bob',
        content='bar',
        user_id='5',
        )]
    self.sources[0].set_comments(self.comments)

    self.comment_resources = [
      {'id': '123',
        'object': {'content': 'foo'},
        'actor': {'id': '4', 'displayName': 'fred', 'url': 'http://fred'},
        'published': '1970-01-01T00:00:01.01Z',
       },
      {'id': '789',
       'object': {'content': 'bar'},
       'actor': {'id': '5', 'displayName': 'bob', 'url': 'http://bob'},
       'published': '1970-01-01T00:00:02.01Z',
       },
      ]
    # (activity id, JSON response) pairs
    self.comments_list_responses = [
      ('1', {'items': [self.comment_resources[0]]}),
      ('2', {'items': [self.comment_resources[1]]}),
      ]

    # TODO: try again soon. difficult to mock the G+ API calls.

  # def test_get_posts_and_get_comments(self):
  #   self.auth_entity.api().
  #   GooglePlusService.call_with_creds(
  #     self.current_user_id, 'activities.list', userId='2468', collection='public',
  #     maxResults=100)\
  #     .AndReturn(self.activities_list_response)
  #   for activity_id, response in self.comments_list_responses:
  #     GooglePlusService.call_with_creds(
  #       self.current_user_id, 'comments.list', activityId=activity_id, maxResults=100)\
  #       .AndReturn(response)
  #   self.mox.ReplayAll()

  #   self.assertEqual(self.activities_with_urls, self.page.get_posts())
  #   self.assert_entities_equal(
  #     self.comments,
  #     self.page.get_comments([(self.activities[3], self.targets[1]),
  #                             (self.activities[4], self.targets[0])]))

  # def test_token_revoked(self):
  #   self.mox.UnsetStubs()  # we want to use GooglePlusService.call()
  #   self.mox.StubOutWithMock(GooglePlusService, 'call')

  #   FakeHttpResponse = collections.namedtuple('FakeHttpResponse', ['status'])
  #   GooglePlusService.call(mox.IgnoreArg(), 'endpoint').AndRaise(
  #     HttpError(FakeHttpResponse(status=404), ''))
  #   self.mox.ReplayAll()

  #   creds = AccessTokenCredentials('token', 'user agent')
  #   CredentialsModel(key_name=self.current_user_id, credentials=creds).save()
  #   self.assertRaises(models.DisableSource, GooglePlusService.call_with_creds,
  #                     self.current_user_id, 'endpoint')
