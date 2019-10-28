Bridgy developer documentation
==============================

Bridgy connects your web site to social media. Likes, retweets,
mentions, cross-posting, and more. `See the user
docs <https://brid.gy/about>`__ for more details, or the `developer
docs <https://bridgy.readthedocs.io/>`__ if you want to contribute.

https://brid.gy/

Bridgy is part of the `IndieWeb <https://indieweb.org/>`__ ecosystem. In
IndieWeb terminology, Bridgy offers
`backfeed <https://indieweb.org/backfeed>`__,
`POSSE <https://indieweb.org/POSSE>`__, and
`webmention <http://indiewebify.me/#send-webmentions>`__ support as a
service.

License: This project is placed in the public domain.

Development
-----------

You’ll need the `App Engine Python
SDK <https://cloud.google.com/appengine/downloads#Google_App_Engine_SDK_for_Python>`__
version 1.9.15 or later (for
`vendor <https://cloud.google.com/appengine/docs/python/tools/libraries27#vendoring>`__
support) or the `Google Cloud
SDK <https://cloud.google.com/sdk/gcloud/>`__ (aka ``gcloud``) with the
``gcloud-appengine-python`` and ``gcloud-appengine-python-extras``
`components <https://cloud.google.com/sdk/docs/components#additional_components>`__.
Add it to your ``$PYTHONPATH``, e.g.
``export PYTHONPATH=$PYTHONPATH:/usr/local/google_appengine``, and then
run:

::

   virtualenv local
   source local/bin/activate
   pip install -r requirements.freeze.txt

   # We install gdata in source mode, and App Engine doesn't follow .egg-link
   # files, so add a symlink to it.
   ln -s ../../../src/gdata/src/gdata local/lib/python2.7/site-packages/gdata
   ln -s ../../../src/gdata/src/atom local/lib/python2.7/site-packages/atom

   python -m unittest discover

The last command runs the unit tests. If you send a pull request, please
include (or update) a test for the new functionality if possible!

To run the entire app locally, run this in the repo root directory:

::

   dev_appserver.py --log_level debug app.yaml background.yaml

If you hit an error during setup, check out the `oauth-dropins
Troubleshooting/FAQ
section <https://github.com/snarfed/oauth-dropins#troubleshootingfaq>`__.
For searchability, here are a handful of error messages that `have
solutions
there <https://github.com/snarfed/oauth-dropins#troubleshootingfaq>`__:

::

   bash: ./bin/easy_install: ...bad interpreter: No such file or directory

   ImportError: cannot import name certs

   ImportError: No module named dev_appserver

   ImportError: cannot import name tweepy

   File ".../site-packages/tweepy/auth.py", line 68, in _get_request_token
     raise TweepError(e)
   TweepError: must be _socket.socket, not socket

   error: option --home not recognized

There’s a good chance you’ll need to make changes to
`granary <https://github.com/snarfed/granary>`__,
`oauth-dropins <https://github.com/snarfed/oauth-dropins>`__, or
`webmention-tools <https://github.com/snarfed/webmention-tools>`__ at
the same time as bridgy. To do that, clone their repos elsewhere, then
install them in “source” mode with:

::

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

The symlinks are necessary because App Engine’s ``vendor`` module
evidently doesn’t follow ``.egg-link`` or ``.pth`` files. :/

To deploy to App Engine, run
`scripts/deploy.sh <https://github.com/snarfed/bridgy/blob/master/scripts/deploy.sh>`__.

`remote_api_shell <https://cloud.google.com/appengine/docs/python/tools/remoteapi#using_the_remote_api_shell>`__
is a useful interactive Python shell that can interact with the
production app’s datastore, memcache, etc. To use it, `create a service
account and download its JSON
credentials <https://console.developers.google.com/project/brid-gy/apiui/credential>`__,
put it somewhere safe, and put its path in your
``GOOGLE_APPLICATION_CREDENTIALS`` environment variable.

Adding a new silo
-----------------

So you want to add a new `silo <http://indiewebcamp.com/silo>`__? Maybe
MySpace, or Friendster, or even Tinder? Great! Here are the steps to do
it. It looks like a lot, but it’s not that bad, honest.

1. Find the silo’s API docs and check that it can do what Bridgy needs.
   At minimum, it should be able to get a user’s posts and their
   comments, likes, and reposts, depending on which of those the silo
   supports. If you want `publish <https://www.brid.gy/about#publish>`__
   support, it should also be able to create posts, comments, likes,
   reposts, and/or RSVPs.
2. Fork and clone this repo.
3. Create an app (aka client) in the silo’s developer console, grab your
   app’s id (aka key) and secret, put them into new local files in the
   repo root dir, `following this
   pattern <https://github.com/snarfed/oauth-dropins/blob/master/oauth_dropins/appengine_config.py>`__.
   You’ll eventually want to send them to @snarfed and @kylewm too, but
   no hurry.
4. Add the silo to
   `oauth-dropins <https://github.com/snarfed/oauth-dropins>`__ if it’s
   not already there:

   1. Add a new ``.py`` file for your silo with an auth model and
      handler classes. Follow the existing examples.
   2. Add a `button
      image <https://github.com/snarfed/oauth-dropins/tree/master/oauth_dropins/static>`__.
   3. Add it to the `app front
      page <https://github.com/snarfed/oauth-dropins/blob/master/templates/index.html>`__
      and the
      `README <https://github.com/snarfed/oauth-dropins/blob/master/README.md>`__.

5. Add the silo to `granary <https://github.com/snarfed/granary>`__:

   1. Add a new ``.py`` file for your silo. Follow the existing
      examples. At minimum, you’ll need to implement
      `get_activities_response <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L137>`__
      and convert your silo’s API data to
      `ActivityStreams <http://activitystrea.ms/>`__.
   2. Add a new unit test file and write some tests!
   3. Add it to
      `api.py <https://github.com/snarfed/granary/blob/master/api.py>`__
      (specifically ``Handler.get``),
      `app.py <https://github.com/snarfed/granary/blob/master/app.py>`__,
      `app.yaml <https://github.com/snarfed/granary/blob/master/app.yaml>`__,
      `index.html <https://github.com/snarfed/granary/blob/master/granary/templates/index.html>`__,
      and the
      `README <https://github.com/snarfed/granary/blob/master/README.md>`__.

6. Add the silo to Bridgy:

   1. Add a new ``.py`` file for your silo with a model class. Follow
      the existing examples.
   2. Add it to
      `app.py <https://github.com/snarfed/bridgy/blob/master/app.py>`__,
      `app.yaml <https://github.com/snarfed/bridgy/blob/master/app.yaml>`__,
      and
      `handlers.py <https://github.com/snarfed/bridgy/blob/master/handlers.py>`__
      (just import the module).
   3. Add a 48x48 PNG icon to
      `static/ <https://github.com/snarfed/bridgy/tree/master/static>`__.
   4. Add a new ``SILO_user.html`` file in
      `templates/ <https://github.com/snarfed/bridgy/tree/master/templates>`__
      and add the silo to
      `index.html <https://github.com/snarfed/bridgy/blob/master/templates/index.html>`__.
      Follow the existing examples.
   5. Add the silo to
      `about.html <https://github.com/snarfed/bridgy/blob/master/templates/about.html>`__
      and this README.
   6. If users’ profile picture URLs can change, add a cron job that
      updates them to
      `cron.py <https://github.com/snarfed/bridgy/blob/master/cron.py>`__
      and
      `cron.yaml <https://github.com/snarfed/bridgy/blob/master/cron.yaml>`__.
      Also add the model class to the datastore backup job there.

7. Optionally add publish support:

   1. Implement
      `create <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L223>`__
      and
      `preview_create <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L247>`__
      for the silo in granary.
   2. Add the silo to
      `publish.py <https://github.com/snarfed/bridgy/blob/master/publish.py>`__:
      import its module, add it to ``SOURCES``, and update `this error
      message <https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/publish.py#L130>`__.
   3. Add a ``publish-signup`` block to ``SILO_user.html`` and add the
      silo to
      `social_user.html <https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/templates/social_user.html#L51>`__.
   4. Update ``app.yaml``.

Good luck, and happy hacking!

Monitoring
----------

App Engine’s `built in
dashboard <https://appengine.google.com/dashboard?&app_id=s~brid-gy>`__
and `log
browser <https://console.developers.google.com/project/brid-gy/logs>`__
are pretty good for interactive monitoring and debugging.

For alerting, we’ve set up `Google Cloud
Monitoring <https://app.google.stackdriver.com/services/app-engine/brid-gy/>`__
(née `Stackdriver <http://en.wikipedia.org/wiki/Stackdriver>`__).
Background in `issue
377 <https://github.com/snarfed/bridgy/issues/377>`__. It `sends
alerts <https://app.google.stackdriver.com/policy-advanced>`__ by email
and SMS when `HTTP 4xx responses average >.1qps or 5xx
>.05qps <https://app.google.stackdriver.com/policy-advanced/650c6f24-17c1-41ac-afda-90a1e56e82c1>`__,
`latency averages
>15s <https://app.google.stackdriver.com/policy-advanced/2c0006f3-7040-4323-b105-8d24b3266ac6>`__,
or `instance count averages
>5 <https://app.google.stackdriver.com/policy-advanced/5cf96390-dc53-4166-b002-4c3b6934f4c3>`__
over the last 15m window.

Stats
-----

I occasionally generate `stats and graphs of usage and
growth <https://snarfed.org/2018-01-02_bridgy-stats-update>`__ from the
`BigQuery
dataset <https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset>`__
(`#715 <https://github.com/snarfed/bridgy/issues/715>`__). Here’s how.

1. `Export the full datastore to Google Cloud
   Storage. <https://cloud.google.com/datastore/docs/export-import-entities>`__
   Include all entities except ``*Auth`` and other internal details.
   Check to see if any new kinds have been added since the last time
   this command was run.

   ::

      gcloud datastore export --async gs://brid-gy.appspot.com/stats/ --kinds Blogger,BlogPost,BlogWebmention,FacebookPage,Flickr,GitHub,GooglePlusPage,Instagram,Medium,Publish,PublishedPage,Response,SyndicatedPost,Tumblr,Twitter,WordPress

   Note that ``--kinds`` is required. `From the export
   docs <https://cloud.google.com/datastore/docs/export-import-entities#limitations>`__,
   *Data exported without specifying an entity filter cannot be loaded
   into BigQuery.*
2. Wait for it to be done with
   ``gcloud datastore operations list | grep done``.
3. `Import it into
   BigQuery <https://cloud.google.com/bigquery/docs/loading-data-cloud-datastore#loading_cloud_datastore_export_service_data>`__:

   ::

      for kind in BlogPost BlogWebmention Publish Response SyndicatedPost; do
        bq load --replace --nosync --source_format=DATASTORE_BACKUP datastore.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
      done

      for kind in Blogger FacebookPage Flickr GitHub GooglePlusPage Instagram Medium Tumblr Twitter WordPress; do
        bq load --replace --nosync --source_format=DATASTORE_BACKUP sources.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
      done

4. Check the jobs with ``bq ls -j``, then wait for them with
   ``bq wait``.
5. `Run the full stats BigQuery
   query. <https://console.cloud.google.com/bigquery?sq=586366768654:9d8d4c13e988477bb976a5e29b63da3b>`__
   Download the results as CSV.
6. `Open the stats
   spreadsheet. <https://docs.google.com/spreadsheets/d/1VhGiZ9Z9PEl7f9ciiVZZgupNcUTsRVltQ8_CqFETpfU/edit>`__
   Import the CSV, replacing the *data* sheet.
7. Check out the graphs! Save full size images with OS or browser
   screenshots, thumbnails with the *Save Image* button. Then post them!

Misc
----

The datastore is automatically backed up by an App Engine cron job that
runs `Datastore managed
export <https://cloud.google.com/datastore/docs/schedule-export>`__
(`details <https://cloud.google.com/datastore/docs/export-import-entities>`__)
and stores the results in `Cloud
Storage <https://developers.google.com/storage/docs/>`__, in the
`brid-gy.appspot.com
bucket <https://console.developers.google.com/project/apps~brid-gy/storage/brid-gy.appspot.com/>`__.
It backs up weekly and includes all entities except ``Response`` and
``SyndicatedPost``, since they make up 92% of all entities by size and
they aren’t as critical to keep.

(We used to use `Datastore Admin
Backup <https://cloud.google.com/appengine/docs/standard/python/console/datastore-backing-up-restoring>`__,
but `it shut down in Feb
2019 <https://cloud.google.com/appengine/docs/deprecations/datastore-admin-backups.>`__

We use this command to set a `Cloud Storage lifecycle
policy <https://developers.google.com/storage/docs/lifecycle>`__ on that
bucket that prunes older backups:

::

   gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com

Run this to see how much space we’re currently using:

::

   gsutil du -hsc gs://brid-gy.appspot.com/\*

Run this to download a single complete backup:

::

   gsutil -m cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .

Also see the `BigQuery
dataset <https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset>`__
(`#715 <https://github.com/snarfed/bridgy/issues/715>`__).
