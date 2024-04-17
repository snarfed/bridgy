<img src="https://raw.github.com/snarfed/bridgy/main/static/bridgy_logo_with_alpha.png" alt="Bridgy" width="128" /> [Bridgy](https://brid.gy/) [![Circle CI](https://circleci.com/gh/snarfed/bridgy.svg?style=svg)](https://circleci.com/gh/snarfed/bridgy) [![Coverage Status](https://coveralls.io/repos/github/snarfed/bridgy/badge.svg)](https://coveralls.io/github/snarfed/bridgy)
===

Bridgy connects your web site to social media. Likes, reposts, mentions, cross-posting, and more. [See the user docs](https://brid.gy/about) for more details, or the [developer docs](https://bridgy.readthedocs.io/) if you want to contribute.

https://brid.gy/

Bridgy is part of the [IndieWeb](https://indieweb.org/) ecosystem. In IndieWeb terminology, Bridgy offers [backfeed](https://indieweb.org/backfeed), [POSSE](https://indieweb.org/POSSE), and [webmention](http://indiewebify.me/#send-webmentions) support as a service.

License: This project is placed in the public domain. You may also use it under the [CC0 License](https://creativecommons.org/publicdomain/zero/1.0/).


Development
---
Pull requests are welcome! Feel free to [ping me in #indieweb-dev](https://indieweb.org/discuss) with any questions.

First, fork and clone this repo. Then, install the [Google Cloud SDK](https://cloud.google.com/sdk/) and run `gcloud components install cloud-firestore-emulator` to install the [Firestore emulator](https://cloud.google.com/firestore/docs/emulator). Once you have them, set up your environment by running these commands in the repo root directory:

```sh
gcloud config set project brid-gy
python3 -m venv local
source local/bin/activate
pip install -r requirements.txt
# needed to serve static files locally
ln -s local/lib/python3*/site-packages/oauth_dropins/static oauth_dropins_static
```

Now, you can fire up the gcloud emulator and run the tests:

```sh
gcloud emulators firestore start --host-port=:8089 --database-mode=datastore-mode < /dev/null >& /dev/null &
python3 -m unittest discover -s tests -t .
kill %1
```

If you send a pull request, please include or update a test for your new code!

To run the app locally, use [`flask run`](https://flask.palletsprojects.com/en/2.0.x/cli/#run-the-development-server):

```shell
gcloud emulators firestore start --host-port=:8089 --database-mode=datastore-mode < /dev/null >& /dev/null &
GAE_ENV=localdev FLASK_ENV=development flask run -p 8080
```

Open [localhost:8080](http://localhost:8080/) and you should see the Bridgy home page!

To test a poll or propagate task, find the relevant _Would add task_ line in the logs, eg:

```
INFO:root:Would add task: projects//locations/us-central1/queues/poll {'app_engine_http_request': {'http_method': 'POST', 'relative_uri': '/_ah/queue/poll', 'app_engine_routing': {'service': 'background'}, 'body': b'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00', 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}, 'schedule_time': seconds: 1591176072
```

...pull out the `relative_uri` and `body`, and then put them together in a `curl` command against localhost:8080 (but don't run it yet!), eg:

```
curl -d 'source_key=agNhcHByFgsSB1R3aXR0ZXIiCXNjaG5hcmZlZAw&last_polled=1970-01-01-00-00-00' \
  http://localhost:8080/_ah/queue/poll
```

Then, restart the app with `FLASK_APP=background` to run the background task processing service, eg:

```shell
gcloud emulators firestore start --host-port=:8089 --database-mode=datastore-mode
GAE_ENV=localdev FLASK_ENV=development flask run -p 8080
```

Now, run the `curl` command you constructed above.

If you hit an error during setup, check out the [oauth-dropins Troubleshooting/FAQ section](https://github.com/snarfed/oauth-dropins#troubleshootingfaq). For searchability, here are a handful of error messages that [have solutions there](https://github.com/snarfed/oauth-dropins#troubleshootingfaq):

```
bash: ./bin/easy_install: ...bad interpreter: No such file or directory

ImportError: cannot import name certs

ImportError: cannot import name tweepy

File ".../site-packages/tweepy/auth.py", line 68, in _get_request_token
  raise TweepError(e)
TweepError: must be _socket.socket, not socket

error: option --home not recognized
```

There's a good chance you'll need to make changes to [granary](https://github.com/snarfed/granary) or [oauth-dropins](https://github.com/snarfed/oauth-dropins) at the same time as bridgy. To do that, clone their repos elsewhere, then install them in "source" mode with:

```
pip uninstall -y oauth-dropins
pip install -e <path-to-oauth-dropins-repo>
ln -sf <path-to-oauth-dropins-repo>/oauth_dropins/static oauth_dropins_static

pip uninstall -y granary
pip install -e <path to granary>
```

To deploy to App Engine, run [`scripts/deploy.sh`](https://github.com/snarfed/bridgy/blob/main/scripts/deploy.sh).

[`remote_api_shell`](https://cloud.google.com/appengine/docs/python/tools/remoteapi#using_the_remote_api_shell) is a useful interactive Python shell that can interact with the production app's datastore, memcache, etc. To use it, [create a service account and download its JSON credentials](https://console.developers.google.com/project/brid-gy/apiui/credential), put it somewhere safe, and put its path in your `GOOGLE_APPLICATION_CREDENTIALS` environment variable.

Deploying to your own App Engine project can be useful for testing, but is not recommended for production.  To deploy to your own App Engine project, create a project on [gcloud console](https://console.cloud.google.com/) and activate the [Tasks API](https://console.cloud.google.com/apis/api/cloudtasks.googleapis.com).  Initialize the project on the command line using `gcloud config set project <project-name>` followed by `gcloud app create`.  You will need to update  `TASKS_LOCATION` in util.py to match your project's location.  Finally, you will need to add your "background" domain (eg `background.YOUR-APP-NAME.appspot.com`) to OTHER_DOMAINS in util.py and set `host_url` in `tasks.py` to your base app url (eg `app-dot-YOUR-APP-NAME.wn.r.appspot.com`).  Finally, deploy (after testing) with `gcloud -q beta app deploy --no-cache --project YOUR-APP-NAME *.yaml`

To work on the browser extension:

```sh
cd browser-extension
npm install
npm run test
```

To run just one test:

```sh
npm run test -- -t 'part of test name'
```


Browser extension: logs in the JavaScript console
---
If you're working on the browser extension, or [you're sending in a bug report for it,](https://github.com/snarfed/bridgy/issues), its JavaScript console logs are invaluable for debugging. Here's how to get them in Firefox:

1. Open `about:debugging`
2. Click _This Firefox_ on the left
3. Scroll down to Bridgy
4. Click _Inspect_
5. Click on the _Console_ tab

<img src="https://user-images.githubusercontent.com/778068/119147612-9c4d2580-ba00-11eb-8d91-39487a662288.png" />

Here's how to send them in with a bug report:
1. Right click, _Export Visible Messages To_, _File_, save the file.
2. Email the file to bridgy @ ryanb.org. _Do not_ post or attach it to a GitHub issue, or anywhere else public, because it contains sensitive tokens and cookies.

<img src="https://user-images.githubusercontent.com/778068/119147959-e6360b80-ba00-11eb-8e35-647850177f4c.png">


Browser extension: release
---
Here's how to cut a new release of the browser extension and publish it [to addons.mozilla.org](https://addons.mozilla.org/en-US/firefox/addon/bridgy/):

1. `ln -fs manifest.firefox.json manifest.json`
1. Load the extension in Firefox (`about:debugging`). Check that it works.
1. Bump the version in `browser-extension/manifest.json`.
1. Update the Changelog in the README.md section below this one.
1. Build and sign the artifact:
    ```sh
    cd browser-extension/
    npm test
    ./node_modules/web-ext/bin/web-ext.js build
    ```
1. Submit it to AMO.
    ```sh
    # get API secret from Ryan if you don't have it
    ./node_modules/web-ext/bin/web-ext.js sign --api-key user:14645521:476 --api-secret ...

    # If this succeeds, it will say:
    ...
    Your add-on has been submitted for review. It passed validation but could not be automatically signed because this is a listed add-on.
    FAIL
    ...
    ```
    It's usually auto-approved within minutes. [Check the public listing here.](https://addons.mozilla.org/en-US/firefox/addon/bridgy/)

Here's how to publish it [to the Chrome Web Store](https://chrome.google.com/webstore/detail/bridgy/lcpeamdhminbbjdfjbpmhgjgliaknflj):

1. `ln -fs manifest.chrome.json manifest.json`
1. Load the extension in Chrome (`chrome://extensions/`, Developer mode on). Check that it works.
1. Build and sign the artifact:
    ```sh
    cd browser-extension/
    npm test
    ./node_modules/web-ext/bin/web-ext.js build
    ```
1. [Open the console.](https://chrome.google.com/webstore/devconsole/)
1. Open the Bridgy item.
1. Choose _Package_ on the left.
1. Click the _Upload new package_ button.
1. Upload the new version's zip file from `browser-extension/web-ext-artifacts/`.
1. Update the Changelog in the _Description_ box. Leave the rest unchanged.
1. Click _Save draft_, then _Submit for review_.


Browser extension: Changelog
---
0.7.0, 2024-01-03

* Remove Instgram. Their anti-bot defenses have led them to suspend a couple people's accounts for using this extension, so we're disabling it out of an abundance of caution. Sorry for the bad news.

0.6.1, 2022-09-18

* Don't open silo login pages if they're not logged in. This ran at extension startup time, which was mostly harmless in manifest v2 since the background page was persistent stayed loaded, but in manifest v3 it's a service worker or non-persistent background page, which gets unloaded and then reloaded every 5m.

0.6.0, 2022-09-17

* Migrate Chrome ([but not Firefox](https://blog.mozilla.org/addons/2022/05/18/manifest-v3-in-firefox-recap-next-steps/)) [from Manifest v2 to v3](https://developer.chrome.com/docs/extensions/mv3/intro/mv3-migration/#man-sw).

0.5, 2022-07-21

* Update Instagram scraping.

0.4, 2022-01-30

* Fix Instagram comments. Add extra client side API fetch, forward to new Bridgy endpoint.
* Expand error messages in options UI.

0.3.5, 2021-03-04

* Dynamically adjust polling frequency per silo based on how often we're seeing new comments and reactions, how recent the last successful webmention was, etc.

0.3.4, 2021-02-22

* Allow individually enabling or disabling Instagram and Facebook.

0.3.3, 2021-02-20

* Only override requests from the browser extension, not all requests to the silos' domains.

0.3.2, 2021-02-18

* Fix compatibility with Facebook Container Tabs.

0.3.1, 2021-02-17

* Add Facebook support!

0.2.1, 2021-01-09

* Add more details to extensions option page: Instagram login, Bridgy IndieAuth registration, etc.
* Support Firefox's Facebook Container Tabs addon.

0.2, 2021-01-03

* Add IndieAuth login on https://brid.gy/ and token handling.
* Add extension settings page with status info and buttons to login again and poll now.
* Better error handling.

0.1.5, 2020-12-25

* Initial beta release!


Adding a new silo
---
So you want to add a new [silo](http://indiewebcamp.com/silo)? Maybe MySpace, or Friendster, or even Tinder? Great! Here are the steps to do it. It looks like a lot, but it's not that bad, honest.

1. Find the silo's API docs and check that it can do what Bridgy needs. At minimum, it should be able to get a user's posts and their comments, likes, and reposts, depending on which of those the silo supports. If you want [publish](https://www.brid.gy/about#publish) support, it should also be able to create posts, comments, likes, reposts, and/or RSVPs.
1. Fork and clone this repo.
1. Create an app (aka client) in the silo's developer console, grab your app's id (aka key) and secret, put them into new local files in the repo root dir, [following this pattern](https://github.com/snarfed/oauth-dropins/blob/6c3628b76aa198d1f9ea1ce0d49322c74b94eabc/oauth_dropins/twitter_auth.py#L16-L17). You'll eventually want to send them to @snarfed too, but no hurry.
1. Add the silo to [oauth-dropins](https://github.com/snarfed/oauth-dropins) if it's not already there:
    1. Add a new `.py` file for your silo with an auth model and handler classes. Follow the existing examples.
    1. Add a 100 pixel tall [button image](https://github.com/snarfed/oauth-dropins/tree/main/oauth_dropins/static) named `[NAME]_2x.png`, where `[NAME]` is your start handler class's `NAME` constant, eg `'twitter'`.
    1. Add it to the [app front page](https://github.com/snarfed/oauth-dropins/blob/main/templates/index.html) and the [README](https://github.com/snarfed/oauth-dropins/blob/main/README.md).
1. Add the silo to [granary](https://github.com/snarfed/granary):
    1. Add a new `.py` file for your silo. Follow the existing examples. At minimum, you'll need to implement [`get_activities_response`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L137) and convert your silo's API data to [ActivityStreams](http://activitystrea.ms/).
    1. Add a new unit test file and write some tests!
    1. Add it to [`api.py`](https://github.com/snarfed/granary/blob/main/api.py) (specifically `Handler.get`), [`app.py`](https://github.com/snarfed/granary/blob/main/app.py), [`index.html`](https://github.com/snarfed/granary/blob/main/granary/templates/index.html), and the [README](https://github.com/snarfed/granary/blob/main/README.md).
1. Add the silo to Bridgy:
    1. Add a new `.py` file for your silo with a model class. Follow the existing examples.
    1. Add it to [`app.py`](https://github.com/snarfed/bridgy/blob/main/app.py) and [`handlers.py`](https://github.com/snarfed/bridgy/blob/main/handlers.py) (just import the module).
    1. Add a 48x48 PNG icon to [`static/`](https://github.com/snarfed/bridgy/tree/main/static).
    1. Add a new `[SILO]_user.html` file in [`templates/`](https://github.com/snarfed/bridgy/tree/main/templates) and add the silo to [`index.html`](https://github.com/snarfed/bridgy/blob/main/templates/index.html). Follow the existing examples.
    1. Add the silo to [`about.html`](https://github.com/snarfed/bridgy/blob/main/templates/about.html) and this README.
    1. If users' profile picture URLs can change, add a cron job that updates them to [`cron.py`](https://github.com/snarfed/bridgy/blob/main/cron.py).
1. Optionally add publish support:
    1. Implement [`create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L223) and [`preview_create`](https://github.com/snarfed/granary/blob/845afbbd521f7ba43b3339bcc1ce3afddd205047/granary/source.py#L247) for the silo in granary.
    1. Add the silo to [`publish.py`](https://github.com/snarfed/bridgy/blob/main/publish.py): import its module, add it to `SOURCES`, and update [this error message](https://github.com/snarfed/bridgy/blob/424bbb28c769eea5636534aba5791e868d63b987/publish.py#L130).

Good luck, and happy hacking!


Monitoring
---

App Engine's [built in dashboard](https://appengine.google.com/dashboard?&app_id=s~brid-gy) and [log browser](https://console.developers.google.com/project/brid-gy/logs) are pretty good for interactive monitoring and debugging.

For alerting, we've set up [Google Cloud Monitoring](https://app.google.stackdriver.com/services/app-engine/brid-gy/) (née [Stackdriver](http://en.wikipedia.org/wiki/Stackdriver)). Background in [issue 377](https://github.com/snarfed/bridgy/issues/377). It [sends alerts](https://app.google.stackdriver.com/policy-advanced) by email and SMS when [HTTP 4xx responses average >.1qps or 5xx >.05qps](https://app.google.stackdriver.com/policy-advanced/650c6f24-17c1-41ac-afda-90a1e56e82c1), [latency averages >15s](https://app.google.stackdriver.com/policy-advanced/2c0006f3-7040-4323-b105-8d24b3266ac6), or [instance count averages >5](https://app.google.stackdriver.com/policy-advanced/5cf96390-dc53-4166-b002-4c3b6934f4c3) over the last 15m window.


Stats
---
I occasionally generate [stats and graphs of usage and growth](https://snarfed.org/2019-01-02_bridgy-stats-update-4) from the [BigQuery dataset](https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset) ([#715](https://github.com/snarfed/bridgy/issues/715)). Here's how.

1. [Export the full datastore to Google Cloud Storage.](https://cloud.google.com/datastore/docs/export-import-entities) Include all entities except `*Auth`, `Domain` and others with credentials or internal details. Check to see if any new kinds have been added since the last time this command was run.

    ```
    gcloud datastore export --async gs://brid-gy.appspot.com/stats/ --kinds Activity,Blogger,BlogPost,BlogWebmention,Bluesky,Facebook,FacebookPage,Flickr,GitHub,GooglePlusPage,Instagram,Mastodon,Medium,Meetup,Publish,PublishedPage,Reddit,Response,SyndicatedPost,Tumblr,Twitter,WordPress
    ```

    Note that `--kinds` is required. [From the export docs](https://cloud.google.com/datastore/docs/export-import-entities#limitations), _Data exported without specifying an entity filter cannot be loaded into BigQuery._ Also, expect this to cost around $10.
1. Wait for it to be done with `gcloud datastore operations list | grep done` or by watching the [Datastore Import/Export page](https://console.cloud.google.com/datastore/databases/-default-/import-export?project=brid-gy).
1. [Import it into BigQuery](https://cloud.google.com/bigquery/docs/loading-data-cloud-datastore#loading_cloud_datastore_export_service_data):

    ```
    for kind in Activity BlogPost BlogWebmention Publish SyndicatedPost; do
      bq load --replace --nosync --source_format=DATASTORE_BACKUP datastore.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
    done

    for kind in Blogger Bluesky Facebook FacebookPage Flickr GitHub GooglePlusPage Instagram Mastodon Medium Meetup Reddit Tumblr Twitter WordPress; do
      bq load --replace --nosync --source_format=DATASTORE_BACKUP sources.$kind gs://brid-gy.appspot.com/stats/all_namespaces/kind_$kind/all_namespaces_kind_$kind.export_metadata
    done
    ```

Open the Datastore entities page for the `Response` kind, sorted by `updated` ascending, and check out the first few rows: https://console.cloud.google.com/datastore/entities;kind=Response;ns=__$DEFAULT$__;sortCol=updated;sortDir=ASCENDING/query/kind?project=brid-gy

Open the existing `Response` table in BigQuery: https://console.cloud.google.com/bigquery?project=brid-gy&ws=%211m10%211m4%214m3%211sbrid-gy%212sdatastore%213sResponse%211m4%211m3%211sbrid-gy%212sbquxjob_371f97c8_18131ff6e69%213sUS

Update the year in the queries below to three years before this year. Query for the same first few rows sorted by `updated` ascending, check that they're the same:

```
SELECT * FROM `brid-gy.datastore.Response`
WHERE updated >= TIMESTAMP('202X-11-01T00:00:00Z')
ORDER BY updated ASC
LIMIT 10
```

Delete those rows:

```
DELETE FROM `brid-gy.datastore.Response`
WHERE updated >= TIMESTAMP('202X-11-01T00:00:00Z')
```

Load the new `Response` entities into a temporary table:
```
bq load --replace=false --nosync --source_format=DATASTORE_BACKUP datastore.Response-new gs://brid-gy.appspot.com/stats/all_namespaces/kind_Response/all_namespaces_kind_Response.export_metadata
```

Append that table to the existing `Response` table:

```
SELECT
leased_until,
original_posts,
type,
updated,
error,
sent,
skipped,
unsent,
created,
source,
status,
failed,

ARRAY(
  SELECT STRUCT<`string` string, text string, provided string>(a, null, 'string')
  FROM UNNEST(activities_json) as a
 ) AS activities_json,

IF(urls_to_activity IS NULL, NULL,
   STRUCT<`string` string, text string, provided string>
     (urls_to_activity, null, 'string')) AS urls_to_activity,

IF(response_json IS NULL, NULL,
   STRUCT<`string` string, text string, provided string>
     (response_json, null, 'string')) AS response_json,

ARRAY(
  SELECT STRUCT<`string` string, text string, provided string>(x, null, 'string')
  FROM UNNEST(old_response_jsons) as x
) AS old_response_jsons,

__key__,
__error__,
__has_error__

FROM `brid-gy.datastore.Response-new`
```

More => Query settings, Set a destination table for query results, dataset brid-gy.datastore, table Response, Append, check Allow large results, Save, Run.

Open `sources.Facebook`, edit schema, add a `url` field, string, nullable.

1. Check the jobs with `bq ls -j`, then wait for them with `bq wait`.
1. [Run the full stats BigQuery query.](https://console.cloud.google.com/bigquery?sq=586366768654:4205685cc2154f18a665122613c0bc05) Download the results as CSV.
1. [Open the stats spreadsheet.](https://docs.google.com/spreadsheets/d/1VhGiZ9Z9PEl7f9ciiVZZgupNcUTsRVltQ8_CqFETpfU/edit) Import the CSV, replacing the _data_ sheet.
1. Change the underscores in column headings to spaces.
1. Open each sheet, edit the chart, and extend the data range to include all of the new rows.
1. Check out the graphs! Save full size images with OS or browser screenshots, thumbnails with the _Download Chart_ button. Then post them!

Final cleanup: delete the temporary `Response-new` table.


Delete old responses
---
Bridgy's online datastore only keeps responses for a year or two. I garbage collect (ie delete) older responses manually, generally just once a year when I generate statistics (above). All historical responses are kept in [BigQuery](https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset) for long term storage.

I use the [Datastore Bulk Delete Dataflow template](https://cloud.google.com/dataflow/docs/guides/templates/provided-utilities#datastore-bulk-delete) with a GQL query like this. (Update the years below to two years before today.)

```sql
SELECT * FROM Response WHERE updated < DATETIME('202X-11-01T00:00:00Z')
```

I either [use the interactive web UI](https://console.cloud.google.com/dataflow/createjob) or this command line:

```sh
gcloud dataflow jobs run 'Delete Response datastore entities over 1y old'
  --gcs-location gs://dataflow-templates-us-central1/latest/Datastore_to_Datastore_Delete
  --region us-central1
  --staging-location gs://brid-gy.appspot.com/tmp-datastore-delete
  --parameters datastoreReadGqlQuery="SELECT * FROM `Response` WHERE updated < DATETIME('202X-11-01T00:00:00Z'),datastoreReadProjectId=brid-gy,datastoreDeleteProjectId=brid-gy"
```

Expect this to take at least a day or so.

Once it's done, [update the stats constants in `admin.py`](https://github.com/snarfed/bridgy/blob/main/admin.py).


Misc
---

The datastore is [exported to BigQuery](https://console.cloud.google.com/bigquery?p=brid-gy&d=datastore&page=dataset) ([#715](https://github.com/snarfed/bridgy/issues/715)) twice a year.

We use this command to set a [Cloud Storage lifecycle policy](https://developers.google.com/storage/docs/lifecycle) on our buckets to prune older backups and other files:

```
gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy.appspot.com
gsutil lifecycle set cloud_storage_lifecycle.json gs://brid-gy_cloudbuild
gsutil lifecycle set cloud_storage_lifecycle.json gs://staging.brid-gy.appspot.com
gsutil lifecycle set cloud_storage_lifecycle.json gs://us.artifacts.brid-gy.appspot.com
```

[See how much space we're currently using in this dashboard.](https://console.cloud.google.com/monitoring/dashboards/resourceList/gcs_bucket?project=brid-gy) Run this to download a single complete backup:

```
gsutil -m cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .
```
