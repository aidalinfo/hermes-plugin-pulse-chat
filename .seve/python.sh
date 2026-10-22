#!/bin/sh
# Python des jobs Sève (setup, test, gate de seve land).
#
# L'image des jobs n'a ni python3, ni curl, ni apt utilisable (système de
# fichiers en lecture seule) : on y pose un CPython autonome
# (python-build-standalone), telecharge avec node, dans .seve-cache/ du
# workspace, plus les deux dependances des tests (pytest, websockets).
# Idempotent : `setup` le prepare une fois par commit de base, `test` le
# rappelle et ne refait rien s'il est deja la.
set -eu

PY_VERSION=3.12.7
PBS_RELEASE=20241016
DEST=.seve-cache/python

if [ ! -x "$DEST/bin/python3" ]; then
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PY_VERSION}+${PBS_RELEASE}-$(uname -m)-unknown-linux-gnu-install_only.tar.gz"
  mkdir -p .seve-cache
  node -e "fetch(process.argv[1]).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.arrayBuffer()}).then(b=>require('fs').writeFileSync(process.argv[2],Buffer.from(b)))" "$url" .seve-cache/python.tar.gz
  tar xzf .seve-cache/python.tar.gz -C .seve-cache
  rm -f .seve-cache/python.tar.gz
fi

"$DEST/bin/python3" -c "import pytest, websockets" 2>/dev/null \
  || "$DEST/bin/python3" -m pip install -q --disable-pip-version-check pytest websockets
