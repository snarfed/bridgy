Brid.gy ![Brid.gy](https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo_128.jpg)
===

Got a web site? Post links to it on social networks? Wish
[comments/replies](http://indiewebcamp.com/comment),
[likes](http://indiewebcamp.com/like), and
[reshares](http://indiewebcamp.com/repost) showed up on your site too? Brid.gy
copies them back for you.

http://brid.gy/

Brid.gy uses [webmentions](http://www.webmention.org/), which are a part of the
[IndieWeb](http://indiewebcamp.com/) ecosystem, so your site will need to accept
webmentions for Brid.gy to work with it. Check out some of the
[existing implementations](http://indiewebcamp.com/webmention#Implementations)!

License: This project is placed in the public domain.


Development
---
All dependencies are in git submodules. Be sure to run
`git submodule init; git submodule update` after you clone the repo.

The tests require the App Engine SDK and python-mox.


Related work
---
* http://webmention.io/
* https://github.com/vrypan/webmention-tools
* http://indiewebcamp.com/original-post-discovery
* http://indiewebcamp.com/permashortcitation
* http://indiewebcamp.com/Twitter#Why_permashortcitation_instead_of_a_link


TODO
---

* move this todo list to issues :P
* unicode. :/ maybe just need to centralize quote_plus(), etc into
  util.add_query_params?
* replace twitter mentions in activities as well as responses
  e.g. https://www.brid.gy/#twitter-liveink
* use mox.StubOutClassWithMocks:
  * streaming.Stream in twitter_streaming_test.test_update_stream
  * send.WebmentionSend in tasks_test.PropagateTest.make_mocks
* G+ tests for both bridgy and activitystreams-unofficial
* test for activitystreams-unofficial Twitter.fetch_replies()

Lower priority:

* use rel=syndication links to distinguish official POSSE posts from other posts
  that happen to mention a link, and maybe send webmentions for the latter.
  details: http://indiewebcamp.com/original-post-discovery
  i decided against this originally because rel=syndication adoption is low,
  based on an unscientific survey. :P
* am i storing refreshed access tokens? or re-refreshing every time?
* currently getting charged for the backend. switch to a module if it's still
  free? https://appengine.google.com/dashboard?&app_id=s~brid-gy#ae-nav-billing
  B1 backends get 9h free per day. *dynamic* modules get 28h free per day,
  manual only 8h.
  https://developers.google.com/appengine/kb/billing#free_quota_backends
  https://developers.google.com/appengine/docs/python/modules/#scaling_types
* detect updated comments and send new webmentions for them
* implement the other direction: convert incoming webmentions into API calls to
  post them as comments, etc.
* only handle public posts? (need to add privacy/audience detection to
  activitystreams-unofficial)
* clear toast messages?

Optimizations:

* cache some API calls with a short expiration, e.g. twitter mentions
* cache webmention discovery
* cache served MF2 HTML and JSON with a short expiration. ideally include the
  cache expiration in the content.
* ...and/or serve the comments (and activities?) directly from the datastore.
  drawback is that we don't get updated content.
* detect and skip non-HTML links before downloading, maybe with a HEAD request?
  e.g. https://twitter.com/snarfed_org/status/414172539837874176 links to
  https://research.microsoft.com/en-us/people/mickens/thesaddestmoment.pdf
