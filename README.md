Brid.gy ![Brid.gy](https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo_128.jpg)
===

Got a web site? Post links to it on social networks? Wish comments showed up on
your site too? Brid.gy copies them back for you.

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

* store and render 'skipped' targets. (test: http://instagram.com/p/hc1xLpp72X/)
* replace t.co links with url entities
* likes/favorites. based on http://indiewebcamp.com/like and
  http://indiewebcamp.com/responses, it looks like it's just u-like and a
  webmention, similar to a reply and may not even need a u-in-reply-to.
  http://indiewebcamp.com/irc/2013-11-11 , http://indiewebcamp.com/repost .
  test against sandeep.io! http://www.sandeep.io/39
  * facebook: bundled in posts, in the 'likes' field.
    https://developers.facebook.com/docs/reference/api/post/#u_0_3
  * twitter: no way to get favorites from REST API! only streaming API. :(
    https://dev.twitter.com/discussions/661
    https://dev.twitter.com/docs/streaming-apis/messages#Events_event
  * google+: plusoners.selflink
    https://developers.google.com/+/api/latest/activities#resource
  * instagram: likes field and api call
    http://instagram.com/developer/endpoints/likes/#get_media_likes
* reshares/reposts, e.g. retweets. http://indiewebcamp.com/repost .
  looks like it's just a link with u-repost, e.g.
      <a class="u-repost" href="http://www.sandeep.io/39">
  e.g. http://sandeep.shetty.in/2013/06/indieweb-repost-test.html,
  http://www.sandeep.io/35, http://www.sandeep.io/34
  also maybe test against http://barryfrost.com/how-to-comment
  * facebook: didn't find a way
  * twitter: /statuses/retweets/ID
    https://dev.twitter.com/docs/api/1.1/get/statuses/retweets/%3Aid
  * google+: resharers.selflink
    https://developers.google.com/+/api/latest/activities#resource
  * instagram: not a feature. third party apps fake it:
    http://www.geeksugar.com/How-Repost-Instagram-29828579
* implement the other direction: convert incoming webmentions into API calls to
  post them as comments, etc.

Lower priority:

* detect updated comments and send new webmentions for them
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
