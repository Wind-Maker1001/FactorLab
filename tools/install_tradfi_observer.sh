#!/bin/sh
# Install on the Demo host as root; archive is a trusted fixed-revision git archive.
set -eu
archive=$1
revision=$2
python=$3
case "$revision" in *[!a-f0-9]*|'') exit 64;; esac
base=/opt/factorlab/tradfi-observer
release=$base/releases/$revision
# Never unpack over a previous release, including an incomplete installation.
test ! -e "$release"
install -d -o root -g root -m 755 "$release/source"
tar -xf "$archive" -C "$release/source"
"$python" -m venv "$release/venv"
"$release/venv/bin/python" -m pip install "$release/source[cross-market]"
install -d -o factorlab-demo -g factorlab-demo -m 700 /var/lib/factorlab-tradfi-observer /var/lib/factorlab-tradfi-observer/task-mailbox
for unit in factorlab-demo-tradfi-observer.service factorlab-demo-tradfi-health.service factorlab-demo-tradfi-health.timer; do
    install -o root -g root -m 644 "$release/source/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
install -d -o root -g root -m 755 /etc/systemd/system/factorlab-demo-execute.service.d
install -o root -g root -m 644 "$release/source/deploy/systemd/factorlab-demo-execute-task-mailbox.conf" /etc/systemd/system/factorlab-demo-execute.service.d/30-task-mailbox.conf
ln -s "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl daemon-reload
systemctl enable factorlab-demo-tradfi-observer.service
systemctl restart factorlab-demo-tradfi-observer.service
systemctl enable --now factorlab-demo-tradfi-health.timer
