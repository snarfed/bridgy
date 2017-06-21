#!/bin/bash
#
# Checks pre-deploy safeguards - tests, app keys, package versions - then deploys.
#
# Expects that your local bridgy, granary, and oauth-dropins repos are all in
# the same directory, and that you have the App Engine SDK installed.
#
# TODO: check that granary, oauth-dropins, and webutil are up to date.

set -e
src=`dirname $0`/../..

# run unit tests
cd $src/oauth-dropins && source local/bin/activate
python -m unittest discover --pattern="test_*.py"

cd ../granary && source local/bin/activate
python -m unittest discover

cd ../bridgy && source local/bin/activate
python -m unittest discover

# check silo app keys (aka client ids)
md5sum -c keys.md5

# check package versions
missing=`pip freeze -q -r requirements.freeze.txt | join --nocheck-order -v 2 - requirements.freeze.txt`
if [[ "$missing" != "" ]]; then
  echo 'ERROR: Package version mismatch! Expected:'
  echo $missing
  exit 1
fi

echo 'Package versions OK.'

# push commits and deploy!
git push
# set an explicit version since logs.py's log fetching depends on explicitly
# enumerating all versions. otherwise gcloud generates a new timestamp-based
# version by default for every deploy.
gcloud -q app deploy --project brid-gy --version 7 app.yaml
