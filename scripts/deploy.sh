#!/bin/bash
#
# Checks pre-deploy safeguards - tests, app keys, package versions - then deploys.
#
# Expects that your local bridgy, granary, and oauth-dropins repos are all in
# the same directory, and that you have gcloud installed.

set -e
src=`dirname $0`/../..

# run unit tests
pkill datastore || true
gcloud beta emulators datastore start --no-store-on-disk --consistency=1.0 --host-port=localhost:8089 < /dev/null >& /dev/null &
sleep 2s

cd $src/oauth-dropins && source local/bin/activate
python -m unittest discover --pattern="test_*.py"

cd ../granary && source local/bin/activate
python -m unittest discover

cd ../bridgy && source local/bin/activate
python -m unittest discover -s tests -t .

kill %1  # datastore emulator

# check silo app keys (aka client ids)
md5sum -c keys.md5

# # TODO: check package versions
# missing=`pip freeze -q -r requirements.txt | join --nocheck-order -v 2 - requirements.txt`
# if [[ "$missing" != "" ]]; then
#   echo 'ERROR: Package version mismatch! Expected:'
#   echo $missing
#   exit 1
# fi

# echo 'Package versions OK.'

# deploy!
# https://cloud.google.com/sdk/gcloud/reference/app/deploy
gcloud -q beta app deploy --no-cache --project brid-gy *.yaml
