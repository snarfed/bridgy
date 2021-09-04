"""Unit tests for browser.py.
"""
import copy
import datetime
import html

from granary import microformats2
from mox3 import mox
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins.webutil import util

from flask_app import app
import browser
from models import Activity, Domain
from . import testutil
from .testutil import FakeGrSource
import util


class FakeBrowserSource(browser.BrowserSource):
    GR_CLASS = FakeGrSource
    SHORT_NAME = "fbs"
    gr_source = FakeGrSource()

    @classmethod
    def key_id_from_actor(cls, actor):
        return actor["fbs_id"]


class BrowserSourceTest(testutil.AppTest):
    def setUp(self):
        super().setUp()
        self.actor["fbs_id"] = "222yyy"
        self.source = FakeBrowserSource.new(actor=self.actor)
        FakeBrowserSource.gr_source.actor = {}

    def test_new(self):
        self.assertIsNone(self.source.auth_entity)
        self.assertEqual("222yyy", self.source.key.id())
        self.assertEqual("Ryan B", self.source.name)
        self.assertEqual("Ryan B (FakeSource)", self.source.label())

    def test_get_activities_response_activity_id(self):
        Activity(
            id="tag:fa.ke,2013:123", activity_json=json_dumps({"foo": "bar"})
        ).put()

        resp = self.source.get_activities_response(activity_id="123")
        self.assertEqual([{"foo": "bar"}], resp["items"])

    def test_get_activities_response_no_activity_id(self):
        Activity(
            id="tag:fa.ke,2013:123",
            source=self.source.key,
            activity_json=json_dumps({"foo": "bar"}),
        ).put()
        Activity(
            id="tag:fa.ke,2013:456",
            source=self.source.key,
            activity_json=json_dumps({"baz": "biff"}),
        ).put()

        other = FakeBrowserSource.new(actor={"fbs_id": "other"}).put()
        Activity(
            id="tag:fa.ke,2013:789",
            source=other,
            activity_json=json_dumps({"boo": "bah"}),
        ).put()

        resp = self.source.get_activities_response()
        self.assert_equals([{"foo": "bar"}, {"baz": "biff"}], resp["items"])

    def test_get_activities_response_no_stored_activity(self):
        resp = self.source.get_activities_response(activity_id="123")
        self.assertEqual([], resp["items"])

    def test_get_comment(self):
        expected = copy.deepcopy(self.activities[0]["object"]["replies"]["items"][0])
        microformats2.prefix_image_urls(expected, browser.IMAGE_PROXY_URL_BASE)

        got = self.source.get_comment("1_2_a", activity=self.activities[0])
        self.assert_equals(expected, got)

    def test_get_comment_no_matching_id(self):
        self.assertIsNone(self.source.get_comment("333", activity=self.activities[0]))

    def test_get_comment_no_activity_kwarg(self):
        self.assertIsNone(self.source.get_comment("020"))

    def test_get_like(self):
        expected = copy.deepcopy(self.activities[0]["object"]["tags"][0])
        microformats2.prefix_image_urls(expected, browser.IMAGE_PROXY_URL_BASE)

        got = self.source.get_like(
            "unused", "unused", "alice", activity=self.activities[0]
        )
        self.assert_equals(expected, got)

    def test_get_like_no_matching_user(self):
        self.assertIsNone(
            self.source.get_like("unused", "unused", "eve", activity=self.activities[0])
        )

    def test_get_like_no_activity_kwarg(self):
        self.assertIsNone(self.source.get_like("unused", "unused", "alice"))


browser.route(FakeBrowserSource)


class BrowserViewTest(testutil.AppTest):
    def setUp(self):
        super().setUp()

        self.domain = Domain(id="snarfed.org", tokens=["towkin"]).put()
        FakeBrowserSource.gr_source = FakeGrSource()
        self.actor["fbs_id"] = "222yyy"
        self.source = FakeBrowserSource.new(actor=self.actor).put()
        self.auth = f"token=towkin&key={self.source.urlsafe().decode()}"
        self.other_source = FakeBrowserSource(id="333zzz", domains=["foo.com"]).put()

        for a in self.activities:
            a["object"]["author"] = self.actor

        self.activities_no_extras = copy.deepcopy(self.activities)
        for a in self.activities_no_extras:
            del a["object"]["tags"]

        self.activities_no_replies = copy.deepcopy(self.activities_no_extras)
        for a in self.activities_no_replies:
            del a["object"]["replies"]

    def post(self, path_query, auth=True, **kwargs):
        if auth and "?" not in path_query:
            path_query += f"?{self.auth}"
        return self.client.post(f"/fbs/browser/{path_query}", **kwargs)

    def test_status(self):
        resp = self.client.get(f"/fbs/browser/status?{self.auth}")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))

        self.assertEqual(
            {
                "status": "enabled",
                "poll-seconds": FakeBrowserSource.FAST_POLL.total_seconds(),
            },
            resp.json,
        )

    def test_homepage(self):
        resp = self.post("homepage", data="homepage html", auth=False)
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual("snarfed", resp.json)

    def test_homepage_no_logged_in_user(self):
        FakeBrowserSource.gr_source.actor = {}
        resp = self.post("homepage", data="not logged in", auth=False)
        self.assertEqual(400, resp.status_code)
        self.assertIn(
            "Couldn't determine logged in FakeSource user",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_profile_new_user(self):
        self.source.delete()

        self.expect_requests_get("https://snarfed.org/", "")
        self.mox.ReplayAll()

        resp = self.post("profile?token=towkin")

        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals(self.source.urlsafe().decode(), resp.json)

        src = self.source.get()
        self.assertEqual("Ryan B", src.name)
        self.assertEqual(["https://snarfed.org/"], src.domain_urls)
        self.assertEqual(["snarfed.org"], src.domains)

    def test_profile_existing_user_update(self):
        self.assertIsNotNone(self.source.get())
        FakeBrowserSource.gr_source.actor.update(
            {
                "displayName": "Mrs. Foo",
                "image": {"url": "http://foo/img"},
            }
        )

        # for webmention discovery
        self.mox.ReplayAll()

        resp = self.post("profile?token=towkin")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals(self.source.urlsafe().decode(), resp.json)

        src = self.source.get()
        self.assertEqual("Mrs. Foo", src.name)
        self.assertEqual("http://foo/img", src.picture)

    def test_profile_fall_back_to_scraped_to_actor(self):
        self.source.delete()

        self.mox.StubOutWithMock(FakeGrSource, "scraped_to_activities")
        FakeGrSource.scraped_to_activities("").AndReturn(([], None))

        self.expect_requests_get("https://snarfed.org/", "")
        self.mox.ReplayAll()

        resp = self.post("profile?token=towkin")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals(self.source.urlsafe().decode(), resp.json)

        src = self.source.get()
        self.assertEqual("Ryan B", src.name)
        self.assertEqual(["https://snarfed.org/"], src.domain_urls)
        self.assertEqual(["snarfed.org"], src.domains)

    def test_profile_no_scraped_actor(self):
        self.source.delete()
        FakeGrSource.actor = None
        resp = self.post("profile?token=towkin")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
        self.assertIn("Missing actor", html.unescape(resp.get_data(as_text=True)))

    def test_profile_private_account(self):
        FakeBrowserSource.gr_source.actor["to"] = [
            {"objectType": "group", "alias": "@private"}
        ]
        resp = self.post("profile?token=towkin")
        self.assertEqual(400, resp.status_code)
        self.assertIn(
            "Your FakeSource account is private.", resp.get_data(as_text=True)
        )

    def test_profile_missing_token(self):
        resp = self.post("profile", auth=False)
        self.assertEqual(400, resp.status_code)
        self.assertIn("Missing required parameter: token", resp.get_data(as_text=True))

    def test_profile_no_stored_token(self):
        self.domain.delete()
        resp = self.post("profile?token=towkin")
        self.assertEqual(403, resp.status_code)
        self.assertIn(
            "towkin is not authorized for any of: {'snarfed.org'}",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_profile_bad_token(self):
        resp = self.post("profile?token=nope")
        self.assertEqual(403, resp.status_code)
        self.assertIn(
            "nope is not authorized for any of: {'snarfed.org'}",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_feed(self):
        resp = self.post("feed")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual(self.activities_no_replies, util.trim_nulls(resp.json))

    def test_feed_empty(self):
        FakeGrSource.activities = []
        resp = self.post("feed")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual([], resp.json)

    def test_feed_missing_token(self):
        resp = self.post("feed?key={self.source.urlsafe().decode()}")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_feed_bad_token(self):
        resp = self.post(f"feed?token=nope&key={self.source.urlsafe().decode()}")
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))
        self.assertIn(
            "nope is not authorized for any of: ['snarfed.org']",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_feed_missing_key(self):
        resp = self.post("feed?token=towkin")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_feed_bad_key(self):
        resp = self.post("feed?token=towkin&key=asdf")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
        # this comes from util.load_source() since the urlsafe key is malformed
        self.assertIn("Bad value for key", resp.get_data(as_text=True))

    def test_feed_token_domain_not_in_source(self):
        resp = self.post(
            f"feed?token=towkin&key={self.other_source.urlsafe().decode()}"
        )
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))

    def test_post(self):
        resp = self.post("post", data="silowe html")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals(self.activities_no_extras[0], util.trim_nulls(resp.json))

        activities = Activity.query().fetch()
        self.assertEqual(1, len(activities))
        self.assertEqual(self.source, activities[0].source)
        self.assert_equals(
            self.activities_no_extras[0],
            util.trim_nulls(json_loads(activities[0].activity_json)),
        )
        self.assertEqual("silowe html", activities[0].html)

    def test_post_empty(self):
        FakeGrSource.activities = []
        resp = self.post("post")
        self.assertEqual(400, resp.status_code)
        self.assertIn("No FakeSource post found in HTML", resp.get_data(as_text=True))

    def test_post_merge_comments(self):
        # existing activity with two comments
        activity = self.activities_no_extras[0]
        reply = self.activities[0]["object"]["replies"]["items"][0]
        activity["object"]["replies"] = {
            "items": [reply, copy.deepcopy(reply)],
            "totalItems": 2,
        }
        activity["object"]["replies"]["items"][1]["id"] = "abc"
        key = Activity(id=activity["id"], activity_json=json_dumps(activity)).put()

        # scraped activity has different second comment
        activity["object"]["replies"]["items"][1]["id"] = "xyz"
        FakeGrSource.activities = [activity]

        resp = self.post("post")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals(activity, util.trim_nulls(resp.json))

        merged = json_loads(key.get().activity_json)
        replies = merged["object"]["replies"]
        self.assert_equals(3, replies["totalItems"], replies)
        self.assert_equals(
            [reply["id"], "abc", "xyz"], [r["id"] for r in replies["items"]]
        )

    def test_post_missing_key(self):
        resp = self.post("post?token=towkin")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_post_bad_key(self):
        resp = self.post("post?token=towkin&key=asdf")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
        # this comes from util.load_source() since the urlsafe key is malformed
        self.assertIn("Bad value for key", resp.get_data(as_text=True))

    def test_post_missing_token(self):
        resp = self.post(f"post?key={self.source.urlsafe().decode()}")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
        self.assertIn("Missing required parameter: token", resp.get_data(as_text=True))

    def test_post_bad_token(self):
        resp = self.post(f"post?token=nope&key={self.source.urlsafe().decode()}")
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))
        self.assertIn(
            "nope is not authorized for any of: ['snarfed.org']",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_post_token_domain_not_in_source(self):
        resp = self.post(
            f"post?token=towkin&key={self.other_source.urlsafe().decode()}"
        )
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))

    def test_reactions(self):
        key = Activity(
            id="tag:fa.ke,2013:123_456",
            source=self.source,
            activity_json=json_dumps(self.activities[0]),
        ).put()
        like = FakeBrowserSource.gr_source.like = {
            "objectType": "activity",
            "verb": "like",
            "id": "new",
        }

        resp = self.post(f"reactions?id=tag:fa.ke,2013:123_456&{self.auth}")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assert_equals([like], resp.json)

        stored = json_loads(key.get().activity_json)
        self.assert_equals(
            self.activities[0]["object"]["tags"] + [like], stored["object"]["tags"]
        )

    def test_reactions_bad_id(self):
        resp = self.post(f"reactions?id=789&{self.auth}")
        self.assertEqual(400, resp.status_code)
        self.assertIn("Expected id to be tag URI", resp.get_data(as_text=True))

    def test_reactions_bad_scraped_data(self):
        Activity(
            id="tag:fa.ke,2013:123_456",
            source=self.source,
            activity_json=json_dumps(self.activities[0]),
        ).put()

        bad_json = "<html><not><json>"
        self.mox.StubOutWithMock(FakeGrSource, "merge_scraped_reactions")
        FakeGrSource.merge_scraped_reactions(bad_json, mox.IgnoreArg()).AndRaise(
            (ValueError("fooey"))
        )
        self.mox.ReplayAll()

        resp = self.post(
            f"reactions?id=tag:fa.ke,2013:123_456&{self.auth}", data=bad_json
        )
        self.assertEqual(400, resp.status_code)
        self.assertIn(
            "Couldn't parse scraped reactions: fooey",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_reactions_no_activity(self):
        resp = self.post(f"reactions?id=tag:fa.ke,2013:789&{self.auth}")
        self.assertEqual(404, resp.status_code)
        self.assertIn(
            "No FakeSource post found for id tag:fa.ke,2013:789",
            resp.get_data(as_text=True),
        )

    def test_reactions_missing_token(self):
        resp = self.post(f"reactions?key={self.source.urlsafe().decode()}")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_reactions_bad_token(self):
        resp = self.post(f"reactions?token=nope&key={self.source.urlsafe().decode()}")
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))
        self.assertIn(
            "nope is not authorized for any of: ['snarfed.org']",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_reactions_missing_key(self):
        resp = self.post("reactions?token=towkin")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_reactions_bad_key(self):
        resp = self.post("reactions?token=towkin&key=asdf")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_reactions_token_domain_not_in_source(self):
        resp = self.post(
            f"reactions?token=towkin&key={self.other_source.urlsafe().decode()}"
        )
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))

    def test_reactions_wrong_activity_source(self):
        Activity(id="tag:fa.ke,2013:123_456", source=self.other_source).put()
        resp = self.post(f"reactions?id=tag:fa.ke,2013:123_456&{self.auth}")
        self.assertEqual(403, resp.status_code)
        self.assertIn(
            "tag:fa.ke,2013:123_456 is owned by Key('FakeBrowserSource', '333zzz')",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_poll(self):
        self.expect_task(
            "poll",
            eta_seconds=0,
            source_key=self.source,
            last_polled="1970-01-01-00-00-00",
        )
        self.mox.ReplayAll()
        resp = self.post("poll")
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual("OK", resp.json)

    def test_poll_missing_token(self):
        resp = self.post("poll?key={self.source.urlsafe().decode()}")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_poll_bad_token(self):
        resp = self.post(f"poll?token=nope&key={self.source.urlsafe().decode()}")
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))
        self.assertIn(
            "nope is not authorized for any of: ['snarfed.org']",
            html.unescape(resp.get_data(as_text=True)),
        )

    def test_poll_missing_key(self):
        resp = self.post("poll?token=towkin")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_poll_bad_key(self):
        resp = self.post("poll?token=towkin&key=asdf")
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))

    def test_poll_token_domain_not_in_source(self):
        resp = self.post(
            f"poll?token=towkin&key={self.other_source.urlsafe().decode()}"
        )
        self.assertEqual(403, resp.status_code, resp.get_data(as_text=True))

    def test_token_domains(self):
        resp = self.post("token-domains?token=towkin")
        self.assertEqual(200, resp.status_code)
        self.assertEqual(["snarfed.org"], resp.json)

    def test_token_domains_missing(self):
        resp = self.post("token-domains?token=unknown")
        self.assertEqual(404, resp.status_code)
