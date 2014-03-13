![Bridgy](https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo_thumb.jpg) Bridgy
===

Got a web site? Post links to it on social networks? Wish
[comments/replies](http://indiewebcamp.com/comment),
[likes](http://indiewebcamp.com/like), and
[reshares](http://indiewebcamp.com/repost) showed up on your site too? Bridgy
copies them back for you.

http://brid.gy/

Bridgy uses [webmentions](http://www.webmention.org/), which are a part of the
[IndieWeb](http://indiewebcamp.com/) ecosystem, so your site will need to accept
webmentions for Bridgy to work with it. Check out some of the
[existing implementations](http://indiewebcamp.com/webmention#Implementations)!

License: This project is placed in the public domain.


Development
---
All dependencies are in git submodules. Be sure to run
`git submodule init; git submodule update` after you clone the repo.

The tests require the App Engine SDK and python-mox.

Deploy command:

```shell
./alltests.py && cd activitystreams && ./alltests.py && cd .. && \
  git push && ~/google_appengine/appcfg.py --oauth2 update .
```


Misc
---
Here are
[remote_api_shell](https://developers.google.com/appengine/articles/remote_api)
and shell commands for generating the statistics published at
[brid.gy/about#stats](http://brid.gy/about#stats):

```py
# remote_api_shell
from models import Response
cursor = None
with open('sent_urls', 'w') as sent, open('unsent_urls', 'w') as unsent:
  while True:
    results, cursor, _ = Response.query(
      projection=[Response.sent,Response.skipped,Response.error,Response.failed]
      ).fetch_page(100, start_cursor=cursor)
    if not results:
      break
    for r in results:
      print >> sent, '\n'.join(r.sent)
      print >> unsent, '\n'.join(r.skipped + r.error + r.failed)

# shell
sort sent_urls  | uniq > sent_uniq
cut -f3 -d/ sent_uniq | sed 's/^www\.//' | sort --ignore-case | uniq -i > sent_domains
wc sent_urls sent_uniq sent_domains
```


Related work
---
* http://webmention.io/
* https://github.com/vrypan/webmention-tools
* http://indiewebcamp.com/original-post-discovery
* http://indiewebcamp.com/permashortcitation
* http://indiewebcamp.com/Twitter#Why_permashortcitation_instead_of_a_link
