#!/bin/bash
#
# Preprocesses docs and runs Sphinx (apidoc and build) to build the HTML docs.
set -e

absfile=`readlink -f $0`
cd `dirname $absfile`

cp -f ../requirements.txt ./requirements.txt
echo 'appengine-sdk>=1.9.40.post0' >> ./requirements.txt

# sphinx-apidoc -f -o source ../ ../tests

rm -f index.rst
cat > index.rst <<EOF
Bridgy developer documentation
==============================

EOF

tail -n +4 ../README.md \
  | pandoc --from=markdown --to=rst \
  | sed -E 's/```/`/; s/`` </ </' \
  >> index.rst

source ../local/bin/activate

# Run sphinx in the virtualenv's python interpreter so it can import packages
# installed in the virtualenv.
python `which sphinx-build` -b html . _build/html
