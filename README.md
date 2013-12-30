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

* currently getting charged for the backend. switch to a module if it's still
  free? https://appengine.google.com/dashboard?&app_id=s~brid-gy#ae-nav-billing
  B1 backends get 9h free per day. *dynamic* modules get 28h free per day,
  manual only 8h.
  https://developers.google.com/appengine/kb/billing#free_quota_backends
  https://developers.google.com/appengine/docs/python/modules/#scaling_types
* target=DEFAULT_... uses brid-gy.appspot.com for source URLs, not www.brid.gy
* catch exceptions when sending individual webmentions, mark as error and continue
https://www.brid.gy/log?start_time=1388420220&key=aglzfmJyaWQtZ3lyRwsSCFJlc3BvbnNlIjl0YWc6dHdpdHRlci5jb20sMjAxMzo0MTc2NzYzMDYxNTE5MTE0MjRfZmF2b3JpdGVkX2J5XzQ2NzcM
* only enable httplib socket API in backend.yaml, not in app?
* fix /_ah/stop in twitter_streaming backend. (it serves a 500 because the
  backend only serves one request at a time, and /_ah/start never returns)
* test blacklist, both poll and propagate
* twitter_streaming test
* replace t.co links with url entities
* move fetching replies (the fetch_replies kwarg to get_activities()) to
  activitystreams-unofficial
* handle 401 Unauthorized response from Twitter in Poll and disable source

Lower priority:

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
