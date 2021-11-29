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

You’ll need the `Google Cloud
SDK <https://cloud.google.com/sdk/gcloud/>`__ (aka ``gcloud``) with the
``gcloud-appengine-python``, ``gcloud-appengine-python-extras`` and
``google-cloud-sdk-datastore-emulator``
`components <https://cloud.google.com/sdk/docs/components#additional_components>`__.
Then, create a Python 3 virtualenv and install the dependencies with:

.. code:: sh

   python3 -m venv local
   source local/bin/activate
   pip install -r requirements.txt
   # needed to serve static file handlers locally
   ln -s local/lib/python3*/site-packages/oauth_dropins/static oauth_dropins_static
   gcloud config set project brid-gy

Now, you can fire up the gcloud emulator and run the tests:

.. code:: sh

   gcloud beta emulators datastore start --no-store-on-disk --consistency=1.0 --host-port=localhost:8089 < /dev/null >& /dev/null
   python3 -m unittest discover -s tests -t .
   kill %1

If you send a pull request, please include or update a test for your new
code!

To test a poll or propagate task, find the relevant *Would add task*
line in the logs, eg:

::

   INFO:root:Would add task: projects//locations/us-central1/queues/poll {'app_engine_http_request': {'http_method': 'POST', 'relative_uri': '/_ah/queue/poll', 'app_engine_routing': {'service': 'background'}, 'body': b'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00', 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}, 'schedule_time': seconds: 1591176072

…pull out the ``relative_uri`` and ``body``, and then put them together
in a ``curl`` command against the ``background`` service, which usually
runs on http://localhost:8081/, eg:

::

   curl -d 'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00' \
     http://localhost:8081/_ah/queue/poll

To run the entire app locally in
`app_server <https://github.com/XeoN-GHMB/app_server>`__ (`which
also serves the static file
handlers <https://groups.google.com/d/topic/google-appengine/BJDE8y2KISM/discussion>`__),
run this in the repo root directory:

.. code:: shell

   app_server -A oauth-dropins .

Open `localhost:8080 <http://localhost:8080/>`__ and you should see the
Bridgy home page!

If you hit an error during setup, check out the `oauth-dropins
Troubleshooting/FAQ
section <https://github.com/snarfed/oauth-dropins#troubleshootingfaq>`__.
For searchability, here are a handful of error messages that `have
solutions
there <https://github.com/snarfed/oauth-dropins#troubleshootingfaq>`__:

::

   bash: ./bin/easy_install: ...bad interpreter: No such file or directory

   ImportError: cannot import name certs

   ImportError: cannot import name tweepy

   File ".../site-packages/tweepy/auth.py", line 68, in _get_request_token
     raise TweepError(e)
   TweepError: must be _socket.socket, not socket

   error: option --home not recognized

There’s a good chance you’ll need to make changes to
`granary <https://github.com/snarfed/granary>`__ or
`oauth-dropins <https://github.com/snarfed/oauth-dropins>`__ at the same
time as bridgy. To do that, clone their repos elsewhere, then install
them in “source” mode with:

::

   pip uninstall -y oauth-dropins
   pip install -e <path-to-oauth-dropins-repo>
   ln -sf <path-to-oauth-dropins-repo>/oauth_dropins/static oauth_dropins_static

   pip uninstall -y granary
   pip install -e <path to granary>

To deploy to App Engine, run
`scripts/deploy.sh <https://github.com/snarfed/bridgy/blob/main/scripts/deploy.sh>`__.

`remote_api_shell <https://cloud.google.com/appengine/docs/python/tools/remoteapi#using_the_remote_api_shell>`__
is a useful interactive Python shell that can interact with the
production app’s datastore, memcache, etc. To use it, `create a service
account and download its JSON
credentials <https://console.developers.google.com/project/brid-gy/apiui/credential>`__,
put it somewhere safe, and put its path in your
``GOOGLE_APPLICATION_CREDENTIALS`` environment variable.

Deploying to your own app-engine project can be useful for testing, but
is not recommended for production. To deploy to your own app-engine
project, create a project on `gcloud
console <https://console.cloud.google.com/>`__ and activate the `Tasks
API <https://console.cloud.google.com/apis/api/cloudtasks.googleapis.com>`__.
Initialize the project on the command line using
``gcloud config set project <project-name>`` followed by
``gcloud app create``. You will need to update ``TASKS_LOCATION`` in
util.py to match your project’s location. Finally, you will need to add
your “background” domain (eg ``background.YOUR-APP-NAME.appspot.com``)
to OTHER_DOMAINS in util.py and set ``host_url`` in ``tasks.py`` to your
base app url (eg ``app-dot-YOUR-APP-NAME.wn.r.appspot.com``). Finally,
deploy (after testing) with
``gcloud -q beta app deploy --no-cache --project YOUR-APP-NAME *.yaml``

To work on the browser extension:

.. code:: sh

   cd browser-extension
   npm install
   npm run test

You need to be logged into Instagram in your browser. The extension
doesn’t have a UI, but you can see what it’s doing on your Bridgy user
page, eg brid.gy/instagram/[username]. Note that it doesn’t work with
`Firefox’s Facebook Container
tabs <https://github.com/mozilla/contain-facebook>`__ add-on. If you
have that enabled, you’ll need to disable it to use Bridgy’s browser
extension.

Extension logs in the JavaScript console
----------------------------------------

If you’re working on the browser extension, or `you’re sending in a bug
report for it, <https://github.com/snarfed/bridgy/issues>`__, its
JavaScript console logs are invaluable for debugging. Here’s how to get
them in Firefox:

Thanks for trying! And for offering to send logs, those would definitely
be helpful. Here’s how to get them: 1. Open ``about:debugging`` 2. Click
*This Firefox* on the left 3. Scroll down to Bridgy 4. Click *Inspect*
5. Click on the *Console* tab

Here’s how to send them in with a bug report: 1. Right click, *Export
Visible Messages To*, *File*, save the file. 2. Email the file to bridgy
@ ryanb.org. *Do not* post or attach it to a GitHub issue, or anywhere
else public, because it contains sensitive tokens and cookies.

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
   pattern <https://github.com/snarfed/oauth-dropins/blob/6c3628b76aa198d1f9ea1ce0d49322c74b94eabc/oauth_dropins/twitter_auth.py#L16-L17>`__.
   You’ll eventually want to send them to @snarfed too, but no hurry.
4. Add the silo to
   `oauth-dropins <https://github.com/snarfed/oauth-dropins>`__ if it’s
   not already there:

   1. Add a new ``.py`` file for your silo with an auth model and
      handler classes. Follow the existing examples.
   2. Add a 100 pixel tall `button
      image <https://github.com/snarfed/oauth-dropins/tree/main/oauth_dropins/static>`__
      named ``[NAME]_2x.png``, where ``[NAME]`` is your start handler
      class’s ``NAME`` constant, eg ``'twitter'``.
   3. Add it to the `app front
      page <https://github.com/snarfed/oauth-dropins/blob/main/templates/index.html>`__
      and the
      `README <https://github.com/snarfed/oauth-dropins/blob/main/README.md>`__.

5. Add the silo to `granary <https://github.com/snarfed/granary>`__:

   1. Add a new ``.py`` file for your silo. Follow the existing
      examples. At minimum, you’ll need to implement
      `get_activities_response <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L137>`__
      and convert your silo’s API data to
      `ActivityStreams <http://activitystrea.ms/>`__.
   2. Add a new unit test file and write some tests!
   3. Add it to
      `api.py <https://github.com/snarfed/granary/blob/main/api.py>`__
      (specifically ``Handler.get``),
      `app.py <https://github.com/snarfed/granary/blob/main/app.py>`__,
      `index.html <https://github.com/snarfed/granary/blob/main/granary/templates/index.html>`__,
      and the
      `README <https://github.com/snarfed/granary/blob/main/README.md>`__.

6. Add the silo to Bridgy:

   1. Add a new ``.py`` file for your silo with a model class. Follow
      the existing examples.
   2. Add it to
      `app.py <https://github.com/snarfed/bridgy/blob/main/app.py>`__
      and
      `handlers.py <https://github.com/snarfed/bridgy/blob/main/handlers.py>`__
      (just import the module).
   3. Add a 48x48 PNG icon to
      `static/ <https://github.com/snarfed/bridgy/tree/main/static>`__.
   4. Add a new ``[SILO]_user.html`` file in
      `templates/ <https://github.com/snarfed/bridgy/tree/main/templates>`__
      and add the silo to
      `index.html <https://github.com/snarfed/bridgy/blob/main/templates/index.html>`__.
      Follow the existing examples.
   5. Add the silo to
      `about.html <https://github.com/snarfed/bridgy/blob/main/templates/about.html>`__
      and this README.
   6. If users’ profile picture URLs can change, add a cron job that
      updates them to
      `cron.py <https://github.com/snarfed/bridgy/blob/main/cron.py>`__.

7. Optionally add publish support:

   1. Implement
      `create <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L223>`__
      and
      `preview_create <https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L247>`__
      for the silo in granary.
   2. Add the silo to
      `publish.py <https://github.com/snarfed/bridgy/blob/main/publish.py>`__:
      import its module, add it to ``SOURCES``, and update `this error
      message <https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/publish.py#L130>`__.

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
growth <https://snarfed.org/2019-01-02_bridgy-stats-update-4>`__ from
the `BigQuery
dataset <https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset>`__
(`#715 <https://github.com/snarfed/bridgy/issues/715>`__). Here’s how.

1. `Export the full datastore to Google Cloud
   Storage. <https://cloud.google.com/datastore/docs/export-import-entities>`__
   Include all entities except ``*Auth`` and other internal details.
   Check to see if any new kinds have been added since the last time
   this command was run.

   ::

      gcloud datastore export --async gs://brid-gy.appspot.com/stats/ --kinds Activity, Blogger,BlogPost,BlogWebmention,Facebook,FacebookPage,Flickr,GitHub,GooglePlusPage,Instagram,Mastodon,Medium,Meetup,Publish,PublishedPage,Reddit,Response,SyndicatedPost,Tumblr,Twitter,WordPress

   Note that ``--kinds`` is required. `From the export
   docs <https://cloud.google.com/datastore/docs/export-import-entities#limitations>`__,
   *Data exported without specifying an entity filter cannot be loaded
   into BigQuery.*

2. Wait for it to be done with
   ``gcloud datastore operations list | grep done``.

3. `Import it into
   BigQuery <https://cloud.google.com/bigquery/docs/loading-data-cloud-datastore#loading_cloud_datastore_export_service_data>`__:

   ::

      for kind in Activity BlogPost BlogWebmention Publish Response SyndicatedPost; do
        bq load --replace --nosync --source_format=DATASTORE_BACKUP datastore.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
      done

      for kind in Blogger Facebook FacebookPage Flickr GitHub GooglePlusPage Instagram Mastodon Medium Meetup Reddit Tumblr Twitter WordPress; do
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
   screenshots, thumbnails with the *Download Chart* button. Then post
   them!

Delete old responses
--------------------

Bridgy only keeps responses that are over a year or two old. I garbage
collect (ie delete) older responses manually, generally just once a year
when I generate statistics (above).

I use the `Datastore Bulk Delete Dataflow
template <https://cloud.google.com/dataflow/docs/guides/templates/provided-utilities#datastore-bulk-delete>`__
with this GQL query:

.. code:: sql

   SELECT * FROM `Response` WHERE updated < DATETIME('2020-11-01T00:00:00Z')

I either `use the interactive web
UI <https://console.cloud.google.com/dataflow/createjob?_ga=2.30358207.1290853518.1636209407-621750517.1595350949>`__
or this command line:

.. code:: sh

   gcloud dataflow jobs run 'Delete Response datastore entities over 1y old'
     --gcs-location gs://dataflow-templates-us-central1/latest/Datastore_to_Datastore_Delete
     --region us-central1
     --staging-location gs://brid-gy.appspot.com/tmp-datastore-delete
     --parameters datastoreReadGqlQuery="SELECT * FROM `Response` WHERE updated < DATETIME('2020-11-01T00:00:00Z'),datastoreReadProjectId=brid-gy,datastoreDeleteProjectId=brid-gy"

Misc
----

The datastore is `exported to
BigQuery <https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset>`__
(`#715 <https://github.com/snarfed/bridgy/issues/715>`__) twice a year.

We use this command to set a `Cloud Storage lifecycle
policy <https://developers.google.com/storage/docs/lifecycle>`__ on our
buckets to prune older backups and other files:

::

   gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com
   gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy_cloudbuild
   gsutil lifecycle set cloud_storage_lifecycle.json gs://staging.brid-gy.appspot.com
   gsutil lifecycle set cloud_storage_lifecycle.json gs://us.artifacts.brid-gy.appspot.com

`See how much space we’re currently using in this
dashboard. <https://console.cloud.google.com/monitoring/dashboards/resourceList/gcs_bucket?project=brid-gy>`__
Run this to download a single complete backup:

::

   gsutil -m cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .
