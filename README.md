![Bridgy](https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo_thumb.jpg) [Bridgy](http://brid.gy/)
===

Got a web site? Want social network replies and likes on your site? Want to post and tweet from your site? Bridgy is for you.

http://brid.gy/

Bridgy pulls comments and likes from social networks back to your web site. You
can also use it to publish your posts to those networks.
[See the docs](https://www.brid.gy/about) for more details.

License: This project is placed in the public domain.


Development
---
Most dependencies are in git submodules. Be sure to run
`git submodule update --init --recursive` after you clone the repo.

Requires the [App Engine SDK](https://developers.google.com/appengine/downloads)
and looks for it in the `GAE_SDK_ROOT` environment variable,
`/usr/local/google_appengine`, or `~/google_appengine`, in that order.

You can run the unit tests with `alltests.py`. If you send a pull request,
please include (or update) a test for the new functionality if possible!

This command runs the tests, pushes any changes in your local repo(s), and
deploys to App Engine:

```shell
./alltests.py && cd activitystreams && ./alltests.py && \
  cd oauth_dropins && ./alltests.py && cd webutil && ./alltests.py && \
  ./facebook_test_live.py && cd ../../.. && \
  git push --recurse-submodules=on-demand && ~/google_appengine/appcfg.py update .
```

Most dependencies are clean, but we've made patches to some that we haven't
(yet) tried to push upstream. If we ever switch submodule repos for those
dependencies, make sure the patches are included!

* snarfed/gdata-python-client@fabb6227361612ac4fcb8bef4438719cb00eaa2b
* snarfed/gdata-python-client@8453e3388d152ac650e22d219fae36da56d9a85d


Monitoring
---

App Engine's [built in dashboard](https://appengine.google.com/dashboard?&app_id=s~brid-gy) and [log browser](https://console.developers.google.com/project/brid-gy/logs) are pretty good for interactive monitoring and debugging.

For alerting, we've set up [Google Cloud Monitoring](https://app.google.stackdriver.com/services/app-engine/brid-gy/) (née [Stackdriver](http://en.wikipedia.org/wiki/Stackdriver)). Background in #377. It [sends alerts](https://app.google.stackdriver.com/policy-advanced) by email and SMS when [HTTP 4xx responses average >.1qps or 5xx >.05qps](https://app.google.stackdriver.com/policy-advanced/650c6f24-17c1-41ac-afda-90a1e56e82c1), [latency averages >15s](https://app.google.stackdriver.com/policy-advanced/2c0006f3-7040-4323-b105-8d24b3266ac6), or [instance count averages >5](https://app.google.stackdriver.com/policy-advanced/5cf96390-dc53-4166-b002-4c3b6934f4c3) over the last 15m window.


Misc
---
The datastore is automatically backed up by a
[cron job](https://developers.google.com/appengine/articles/scheduled_backups)
that runs
[Datastore Admin backup](https://developers.google.com/appengine/docs/adminconsole/datastoreadmin#backup_and_restore)
and stores the results in
[Cloud Storage](https://developers.google.com/storage/docs/), in the
[brid-gy.appspot.com bucket](https://console.developers.google.com/project/apps~brid-gy/storage/brid-gy.appspot.com/).
It backs up all entities weekly, and all entities except `Response` and
`SyndicatedPost` daily, since they make up 92% of all entities by size and
they aren't as critical to keep.

We use this command to set a
[Cloud Storage lifecycle policy](https://developers.google.com/storage/docs/lifecycle)
on that bucket that deletes all files over 30 days old:

```
gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com
```

So far, this has kept us within the
[5GB free quota](https://developers.google.com/appengine/docs/quotas#Default_Gcs_Bucket).
Run this command to see how much space we're currently using:

```
gsutil du -hsc gs://brid-gy.appspot.com/\*
```

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
#      projection=[Response.sent,Response.skipped,Response.error,Response.failed]
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


Related projects and docs
---
* http://webmention.io/
* https://github.com/vrypan/webmention-tools
* http://indiewebcamp.com/original-post-discovery
* http://indiewebcamp.com/permashortcitation
* http://indiewebcamp.com/Twitter#Why_permashortcitation_instead_of_a_link
