#!/bin/sh
# Rebuilds requirements.txt (exact versions with hashes) from requirements.in,
# in the same Alpine Python the image uses, so the hashes cover the musl
# wheels for both amd64 and arm64.
#
# To take newer versions of everything, run this and commit the result.
# To take one package, add --upgrade-package NAME to the pip-compile line.
set -eu
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD":/w -w /w python:3.13-alpine sh -c \
  'pip install -q pip-tools >/dev/null 2>&1 &&
   pip-compile -q --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in'
echo "requirements.txt rebuilt. Run the tests and build the image before committing."
