![Bridgy](https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo_thumb.jpg) [Bridgy](https://brid.gy/) [![Circle CI](https://circleci.com/gh/snarfed/bridgy.svg?style=svg)](https://circleci.com/gh/snarfed/bridgy) [![Coverage Status](https://coveralls.io/repos/github/snarfed/bridgy/badge.svg?branch=master)](https://coveralls.io/github/snarfed/bridgy?branch=master)
===

Got a web site? Want social network replies and likes on your site? Want to post and tweet from your site? Bridgy is for you.

https://brid.gy/

Bridgy pulls comments and likes from social networks back to your web site. You
can also use it to publish your posts to those networks.
[See the docs](https://brid.gy/about) for more details.

License: This project is placed in the public domain.


Development
---
You'll need the
[App Engine Python SDK](https://cloud.google.com/appengine/downloads#Google_App_Engine_SDK_for_Python)
version 1.9.15 or later (for
[`vendor`](https://cloud.google.com/appengine/docs/python/tools/libraries27#vendoring)
support). Add it to your `$PYTHONPATH`, e.g.
`export PYTHONPATH=$PYTHONPATH:/usr/local/google_appengine`, and then run:

```
virtualenv local
source local/bin/activate
pip install -r requirements.freeze.txt

# We install gdata in source mode, and App Engine doesn't follow .egg-link
# files, so add a symlink to it.
ln -s ../../../src/gdata/src/gdata local/lib/python2.7/site-packages/gdata
ln -s ../../../src/gdata/src/atom local/lib/python2.7/site-packages/atom

python -m unittest discover
```

The last command runs the unit tests. If you send a pull request, please include
(or update) a test for the new functionality if possible!

If you hit an error during setup, check out the [oauth-dropins Troubleshooting/FAQ section](https://github.com/snarfed/oauth-dropins#troubleshootingfaq). For searchability, here are a handful of error messages that [have solutions there](https://github.com/snarfed/oauth-dropins#troubleshootingfaq):

```
bash: ./bin/easy_install: ...bad interpreter: No such file or directory

ImportError: cannot import name certs

ImportError: No module named dev_appserver

ImportError: cannot import name tweepy

File ".../site-packages/tweepy/auth.py", line 68, in _get_request_token
  raise TweepError(e)
TweepError: must be _socket.socket, not socket

error: option --home not recognized
```

There's a good chance you'll need to make changes to
[granary](https://github.com/snarfed/granary),
[oauth-dropins](https://github.com/snarfed/oauth-dropins), or
[webmention-tools](https://github.com/snarfed/webmention-tools) at the same time
as bridgy. To do that, clone their repos elsewhere, then install them in
"source" mode with:

```
pip uninstall -y oauth-dropins
pip install -e <path to oauth-dropins>
ln -s <path to oauth-dropins>/oauth_dropins \
  local/lib/python2.7/site-packages/oauth_dropins

pip uninstall -y granary
pip install -e <path to granary>
ln -s <path to granary>/granary \
  local/lib/python2.7/site-packages/granary

pip uninstall -y webmentiontools
# webmention-tools isn't in pypi
ln -s <path to webmention-tools>/webmentiontools \
  local/lib/python2.7/site-packages/webmentiontools
```

The symlinks are necessary because App Engine's `vendor` module evidently
doesn't follow `.egg-link` or `.pth` files. :/

To deploy to App Engine, run [`scripts/deploy.sh`](https://github.com/snarfed/bridgy/blob/master/scripts/deploy.sh).

[`remote_api_shell`](https://cloud.google.com/appengine/docs/python/tools/remoteapi#using_the_remote_api_shell)
is a useful interactive Python shell that can interact with the production app's
datastore, memcache, etc. To use it,
[create a service account and download its JSON credentials](https://console.developers.google.com/project/brid-gy/apiui/credential),
put it somewhere safe, and put its path in your `GOOGLE_APPLICATION_CREDENTIALS`
environment variable.


Adding a new silo
---
So you want to add a new [silo](http://indiewebcamp.com/silo)? Maybe MySpace, or
Friendster, or even Tinder? Great! Here are the steps to do it. It looks like a
lot, but it's not that bad, honest.

1. Find the silo's API docs and check that it can do what Bridgy needs. At
minimum, it should be able to get a user's posts and their comments, likes, and
reposts, depending on which of those the silo supports. If you want
[publish](https://www.brid.gy/about#publish) support, it should also be able to
create posts, comments, likes, reposts, and/or RSVPs.
1. Fork and clone this repo.
1. Create an app (aka client) in the silo's developer console, grab your app's id
(aka key) and secret, put them into new local files in the repo root dir,
[following this pattern](https://github.com/snarfed/oauth-dropins/blob/master/oauth_dropins/appengine_config.py).
You'll eventually want to send them to @snarfed and @kylewm too, but no hurry.
1. Add the silo to [oauth-dropins](https://github.com/snarfed/oauth-dropins) if
   it's not already there:
  1. Add a new `.py` file for your silo with an auth model and handler classes.
    Follow the existing examples.
  1. Add a [button image](https://github.com/snarfed/oauth-dropins/tree/master/oauth_dropins/static).
  1. Add it to the
  [app front page](https://github.com/snarfed/oauth-dropins/blob/master/templates/index.html)
  and the [README](https://github.com/snarfed/oauth-dropins/blob/master/README.md).
1. Add the silo to [granary](https://github.com/snarfed/granary):
  1. Add a new `.py` file for your silo. Follow the existing examples. At
     minimum, you'll need to implement
     [`get_activities_response`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L137)
     and convert your silo's API data to [ActivityStreams](http://activitystrea.ms/).
  1. Add a new unit test file and write some tests!
  1. Add it to
  [`activitystreams.py`](https://github.com/snarfed/granary/blob/master/activitystreams.py)
  (specifically `Handler.get`),
  [`app.py`](https://github.com/snarfed/granary/blob/master/app.py),
  [`app.yaml`](https://github.com/snarfed/granary/blob/master/app.yaml),
  [`index.html`](https://github.com/snarfed/granary/blob/master/granary/templates/index.html),
  and the
  [README](https://github.com/snarfed/granary/blob/master/README.md).
1. Add the silo to Bridgy:
  1. Add a new `.py` file for your silo with a model class. Follow the existing
  examples.
  1. Add it to
  [`app.py`](https://github.com/snarfed/bridgy/blob/master/app.py),
  [`app.yaml`](https://github.com/snarfed/bridgy/blob/master/app.yaml), and
  [`handlers.py`](https://github.com/snarfed/bridgy/blob/master/handlers.py),
  (just import the module).
  1. Add a 24x24 PNG icon to [`static/`](https://github.com/snarfed/bridgy/tree/master/static).
  1. Add new `SILO_signup.html` and `SILO_user.html` files in
  [`templates/`](https://github.com/snarfed/bridgy/tree/master/templates).
  and add the silo to
  [`listen_signup.html`](https://github.com/snarfed/bridgy/blob/master/templates/listen_signup.html).
  Follow the existing examples.
  1. Add the silo to
  [`about.html`](https://github.com/snarfed/bridgy/blob/master/templates/about.html) and this README.
  1. If users' profile picture URLs can change, add a cron job that updates them
  to [`cron.py`](https://github.com/snarfed/bridgy/blob/master/cron.py) and
  [`cron.yaml`](https://github.com/snarfed/bridgy/blob/master/cron.yaml). Also
  add the model class to the datastore backup job there.
1. Optionally add publish support:
  1. Implement
  [`create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L223) and
  [`preview_create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L247)
  for the silo in granary.
  1. Add the silo to
  [`publish.py`](https://github.com/snarfed/bridgy/blob/master/publish.py): import its
  module, add it to `SOURCES`, and update
  [this error message](https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/publish.py#L130).
  1. Add a `publish-signup` block to `SILO_user.html` and add the silo
  [to `social_user.html` here](https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/templates/social_user.html#L51).
  1. Update `app.yaml`.

Good luck, and happy hacking!


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
It backs up all entities monthly, and all entities except `Response` and
`SyndicatedPost` weekly, since they make up 92% of all entities by size and
they aren't as critical to keep.

We use this command to set a
[Cloud Storage lifecycle policy](https://developers.google.com/storage/docs/lifecycle)
on that bucket that prunes older backups:

```
gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com
```

Run this to see how much space we're currently using:

```
gsutil du -hsc gs://brid-gy.appspot.com/\*
```

Run this to download a single complete backup, for e.g. generating usage metrics
with [`to_tsv.py`](https://github.com/snarfed/bridgy/blob/master/scripts/to_tsv.py):

```
gsutil -m cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .
```
