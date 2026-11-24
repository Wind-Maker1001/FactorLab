#!/bin/sh
set -eu
revision=$1
case "$revision" in *[!0-9a-f]*|'') exit 78;; esac
[ "${#revision}" = 40 ]
base=/opt/factorlab-alpaca
release=$base/releases/$revision
[ -x "$release/run" ]
# Do not stop the running service for an incompatible downgrade.
sudo -u factorlab-alpaca "$release/venv/bin/python" -c '
import sqlite3, sys
p="file:/var/lib/factorlab-alpaca-paper/runtime.sqlite3?mode=ro"
with sqlite3.connect(p, uri=True) as db:
    version=db.execute("PRAGMA user_version").fetchone()[0]
sys.exit(0 if version == 1 else 78)
'
systemctl stop factorlab-alpaca-paper.service
ln -s "$release" "$base/current.rollback"
mv -Tf "$base/current.rollback" "$base/current"
systemctl start factorlab-alpaca-paper.service
