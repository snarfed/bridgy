<img src="https://raw.github.com/snarfed/bridgy/master/static/bridgy_logo.jpg" alt="Bridgy" width="128" /> [Bridgy](https://brid.gy/) [![Circle CI](https://circleci.com/gh/snarfed/bridgy.svg?style=svg)](https://circleci.com/gh/snarfed/bridgy) [![Coverage Status](https://coveralls.io/repos/github/snarfed/bridgy/badge.svg?branch=master)](https://coveralls.io/github/snarfed/bridgy?branch=master)
===

Bridgy connects your web site to social media. Likes, retweets, mentions, cross-posting, and more. [See the user docs](https://brid.gy/about) for more details, or the [developer docs](https://bridgy.readthedocs.io/) if you want to contribute.

https://brid.gy/

Bridgy is part of the [IndieWeb](https://indieweb.org/) ecosystem. In IndieWeb terminology, Bridgy offers [backfeed](https://indieweb.org/backfeed), [POSSE](https://indieweb.org/POSSE), and [webmention](http://indiewebify.me/#send-webmentions) support as a service.

License: This project is placed in the public domain.


Development
---
You'll need the [Google Cloud SDK](https://cloud.google.com/sdk/gcloud/) (aka `gcloud`) with the `gcloud-appengine-python`, `gcloud-appengine-python-extras` and `google-cloud-sdk-datastore-emulator` [components](https://cloud.google.com/sdk/docs/components#additional_components). Then, create a Python 3 virtualenv and install the dependencies with:

```sh
python3 -m venv local3
source local3/bin/activate
pip install -r requirements.txt
ln -s local3/lib/python3*/site-packages/oauth_dropins  # needed to serve static file assets in dev_appserver
gcloud config set project brid-gy
```

Now, you can fire up the gcloud emulator and run the tests:

```sh
gcloud beta emulators datastore start --no-store-on-disk --consistency=1.0 --host-port=localhost:8089 < /dev/null >& /dev/null
python3 -m unittest discover -s tests -t .
kill %1
```

If you send a pull request, please include or update a test for your new code!

To test a poll or propagate task, find the relevant _Would add task_ line in the logs, eg:

```
INFO:root:Would add task: projects//locations/us-central1/queues/poll {'app_engine_http_request': {'http_method': 'POST', 'relative_uri': '/_ah/queue/poll', 'app_engine_routing': {'service': 'background'}, 'body': b'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00', 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}, 'schedule_time': seconds: 1591176072
```

...pull out the `relative_uri` and `body`, and then put them together in a `curl` command against the `background` service, which usually runs on http://localhost:8081/, eg:

```
curl -d 'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00' \
  http://localhost:8081/_ah/queue/poll
```

To run the entire app locally, run this in the repo root directory:

```
dev_appserver.py --log_level debug --enable_host_checking false \
  --support_datastore_emulator --datastore_emulator_port=8089 \
  --application=brid-gy ~/src/bridgy/app.yaml ~/src/bridgy/background.yaml
```

(Note: dev_appserver.py is incompatible with python3. if python3 is your default python, you can run `python2 /location/of/dev_appserver.py ...` instead.)

Open [localhost:8080](http://localhost:8080/) and you should see the Bridgy home page!

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

There's a good chance you'll need to make changes to [granary](https://github.com/snarfed/granary), [oauth-dropins](https://github.com/snarfed/oauth-dropins), or [webmention-tools](https://github.com/snarfed/webmention-tools) at the same time as bridgy. To do that, clone their repos elsewhere, then install them in "source" mode with:

```
pip uninstall -y oauth-dropins
pip install -e <path-to-oauth-dropins-repo>
ln -sf <path-to-oauth-dropins-repo>/oauth_dropins  # needed to serve static file assets in dev_appserver

pip uninstall -y granary
pip install -e <path to granary>

pip uninstall -y webmentiontools
pip install <path to webmention-tools>
```

To deploy to App Engine, run [`scripts/deploy.sh`](https://github.com/snarfed/bridgy/blob/master/scripts/deploy.sh).

[`remote_api_shell`](https://cloud.google.com/appengine/docs/python/tools/remoteapi#using_the_remote_api_shell) is a useful interactive Python shell that can interact with the production app's datastore, memcache, etc. To use it, [create a service account and download its JSON credentials](https://console.developers.google.com/project/brid-gy/apiui/credential), put it somewhere safe, and put its path in your `GOOGLE_APPLICATION_CREDENTIALS` environment variable.

Deploying to your own app-engine project can be useful for testing, but is not recommended for production.  To deploy to your own app-engine project, create a project on [gcloud console](https://console.cloud.google.com/) and activate the [Tasks API](https://console.cloud.google.com/apis/api/cloudtasks.googleapis.com).  Initialize the project on the command line using `gcloud config set project <project-name>` followed by `gcloud app create`.  You will need to update  `TASKS_LOCATION` in util.py to match your project's location.  Finally, you will need to add your "background" domain (eg `background.YOUR-APP-NAME.appspot.com`) to OTHER_DOMAINS in util.py and set `host_url` in `tasks.py` to your base app url (eg `app-dot-YOUR-APP-NAME.wn.r.appspot.com`).  Finally, deploy (after testing) with `gcloud -q beta app deploy --no-cache --project YOUR-APP-NAME *.yaml`


Adding a new silo
---
So you want to add a new [silo](http://indiewebcamp.com/silo)? Maybe MySpace, or Friendster, or even Tinder? Great! Here are the steps to do it. It looks like a lot, but it's not that bad, honest.

1. Find the silo's API docs and check that it can do what Bridgy needs. At minimum, it should be able to get a user's posts and their comments, likes, and reposts, depending on which of those the silo supports. If you want [publish](https://www.brid.gy/about#publish) support, it should also be able to create posts, comments, likes, reposts, and/or RSVPs.
1. Fork and clone this repo.
1. Create an app (aka client) in the silo's developer console, grab your app's id (aka key) and secret, put them into new local files in the repo root dir, [following this pattern](https://github.com/snarfed/oauth-dropins/blob/6c3628b76aa198d1f9ea1ce0d49322c74b94eabc/oauth_dropins/twitter_auth.py#L16-L17). You'll eventually want to send them to @snarfed too, but no hurry.
1. Add the silo to [oauth-dropins](https://github.com/snarfed/oauth-dropins) if it's not already there:
    1. Add a new `.py` file for your silo with an auth model and handler classes. Follow the existing examples.
    1. Add a 100 pixel tall [button image](https://github.com/snarfed/oauth-dropins/tree/master/oauth_dropins/static) named `[NAME]_2x.png`, where `[NAME]` is your start handler class's `NAME` constant, eg `'twitter'`.
    1. Add it to the [app front page](https://github.com/snarfed/oauth-dropins/blob/master/templates/index.html) and the [README](https://github.com/snarfed/oauth-dropins/blob/master/README.md).
1. Add the silo to [granary](https://github.com/snarfed/granary):
    1. Add a new `.py` file for your silo. Follow the existing examples. At minimum, you'll need to implement [`get_activities_response`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L137) and convert your silo's API data to [ActivityStreams](http://activitystrea.ms/).
    1. Add a new unit test file and write some tests!
    1. Add it to [`api.py`](https://github.com/snarfed/granary/blob/master/api.py) (specifically `Handler.get`), [`app.py`](https://github.com/snarfed/granary/blob/master/app.py), [`index.html`](https://github.com/snarfed/granary/blob/master/granary/templates/index.html), and the [README](https://github.com/snarfed/granary/blob/master/README.md).
1. Add the silo to Bridgy:
    1. Add a new `.py` file for your silo with a model class. Follow the existing examples.
    1. Add it to [`app.py`](https://github.com/snarfed/bridgy/blob/master/app.py) and [`handlers.py`](https://github.com/snarfed/bridgy/blob/master/handlers.py) (just import the module).
    1. Add a 48x48 PNG icon to [`static/`](https://github.com/snarfed/bridgy/tree/master/static).
    1. Add a new `[SILO]_user.html` file in [`templates/`](https://github.com/snarfed/bridgy/tree/master/templates) and add the silo to [`index.html`](https://github.com/snarfed/bridgy/blob/master/templates/index.html). Follow the existing examples.
    1. Add the silo to [`about.html`](https://github.com/snarfed/bridgy/blob/master/templates/about.html) and this README.
    1. If users' profile picture URLs can change, add a cron job that updates them to [`cron.py`](https://github.com/snarfed/bridgy/blob/master/cron.py).
1. Optionally add publish support:
    1. Implement [`create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L223) and [`preview_create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L247) for the silo in granary.
    1. Add the silo to [`publish.py`](https://github.com/snarfed/bridgy/blob/master/publish.py): import its module, add it to `SOURCES`, and update [this error message](https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/publish.py#L130).

Good luck, and happy hacking!


Monitoring
---

App Engine's [built in dashboard](https://appengine.google.com/dashboard?&app_id=s~brid-gy) and [log browser](https://console.developers.google.com/project/brid-gy/logs) are pretty good for interactive monitoring and debugging.

For alerting, we've set up [Google Cloud Monitoring](https://app.google.stackdriver.com/services/app-engine/brid-gy/) (née [Stackdriver](http://en.wikipedia.org/wiki/Stackdriver)). Background in [issue 377](https://github.com/snarfed/bridgy/issues/377). It [sends alerts](https://app.google.stackdriver.com/policy-advanced) by email and SMS when [HTTP 4xx responses average >.1qps or 5xx >.05qps](https://app.google.stackdriver.com/policy-advanced/650c6f24-17c1-41ac-afda-90a1e56e82c1), [latency averages >15s](https://app.google.stackdriver.com/policy-advanced/2c0006f3-7040-4323-b105-8d24b3266ac6), or [instance count averages >5](https://app.google.stackdriver.com/policy-advanced/5cf96390-dc53-4166-b002-4c3b6934f4c3) over the last 15m window.


Stats
---
I occasionally generate [stats and graphs of usage and growth](https://snarfed.org/2019-01-02_bridgy-stats-update-4) from the [BigQuery dataset](https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset) ([#715](https://github.com/snarfed/bridgy/issues/715)). Here's how.

1. [Export the full datastore to Google Cloud Storage.](https://cloud.google.com/datastore/docs/export-import-entities) Include all entities except `*Auth` and other internal details. Check to see if any new kinds have been added since the last time this command was run.

    ```
    gcloud datastore export --async gs://brid-gy.appspot.com/stats/ --kinds Blogger,BlogPost,BlogWebmention,FacebookPage,Flickr,GitHub,GooglePlusPage,Instagram,Mastodon,Medium,Meetup,Publish,PublishedPage,Reddit,Response,SyndicatedPost,Tumblr,Twitter,WordPress
    ```

    Note that `--kinds` is required. [From the export docs](https://cloud.google.com/datastore/docs/export-import-entities#limitations), _Data exported without specifying an entity filter cannot be loaded into BigQuery._
1. Wait for it to be done with `gcloud datastore operations list | grep done`.
1. [Import it into BigQuery](https://cloud.google.com/bigquery/docs/loading-data-cloud-datastore#loading_cloud_datastore_export_service_data):

    ```
    for kind in BlogPost BlogWebmention Publish Response SyndicatedPost; do
      bq load --replace --nosync --source_format=DATASTORE_BACKUP datastore.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
    done

    for kind in Blogger FacebookPage Flickr GitHub GooglePlusPage Instagram Mastodon Medium Meetup Reddit Tumblr Twitter WordPress; do
      bq load --replace --nosync --source_format=DATASTORE_BACKUP sources.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
    done
    ```
1. Check the jobs with `bq ls -j`, then wait for them with `bq wait`.
1. [Run the full stats BigQuery query.](https://console.cloud.google.com/bigquery?sq=586366768654:9d8d4c13e988477bb976a5e29b63da3b) Download the results as CSV.
1. [Open the stats spreadsheet.](https://docs.google.com/spreadsheets/d/1VhGiZ9Z9PEl7f9ciiVZZgupNcUTsRVltQ8_CqFETpfU/edit) Import the CSV, replacing the _data_ sheet.
1. Check out the graphs! Save full size images with OS or browser screenshots, thumbnails with the _Download Chart_ button. Then post them!


Misc
---
The datastore is automatically backed up by an App Engine cron job that runs [Datastore managed export](https://cloud.google.com/datastore/docs/schedule-export) ([details](https://cloud.google.com/datastore/docs/export-import-entities)) and stores the results in [Cloud Storage](https://developers.google.com/storage/docs/), in the [brid-gy.appspot.com bucket](https://console.developers.google.com/project/apps~brid-gy/storage/brid-gy.appspot.com/). It backs up weekly and includes all entities except `Response` and `SyndicatedPost`, since they make up 92% of all entities by size and they aren't as critical to keep.

(We used to use [Datastore Admin Backup](https://cloud.google.com/appengine/docs/standard/python/console/datastore-backing-up-restoring), but [it shut down in Feb 2019](https://cloud.google.com/appengine/docs/deprecations/datastore-admin-backups.).)

We use this command to set a [Cloud Storage lifecycle policy](https://developers.google.com/storage/docs/lifecycle) on that bucket that prunes older backups:

```
gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com
```

Run this to see how much space we're currently using:

```
gsutil du -hsc gs://brid-gy.appspot.com/\*
```

Run this to download a single complete backup:

```
gsutil -m cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .
```

Also see the [BigQuery dataset](https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset) ([#715](https://github.com/snarfed/bridgy/issues/715)).
