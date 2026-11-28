#!/bin/sh
set -eu
# Usage: sudo sh install_alpaca_paper_runtime.sh BUNDLE FULL_COMMIT [PYTHON|auto]
bundle=$1
revision=$2
python=${3:-auto}
if [ "$python" = "auto" ] || [ -z "$python" ]; then
    arch=$(uname -m)
    python="/opt/factorlab/python/cpython-3.13-linux-${arch}-gnu/bin/python3"
fi
case "$revision" in *[!0-9a-f]*|'') printf 'full commit required\n' >&2; exit 78;; esac
[ "${#revision}" = 40 ]
base=/opt/factorlab-alpaca
release=$base/releases/$revision
[ ! -e "$release" ]
if ! getent passwd factorlab-alpaca >/dev/null; then
    useradd --system --home /nonexistent --shell /usr/sbin/nologin factorlab-alpaca
fi
install -d -o factorlab-alpaca -g factorlab-alpaca -m 700 /var/lib/factorlab-alpaca-paper
install -d -o root -g root -m 700 /etc/factorlab-alpaca-paper
install -d -o root -g root -m 755 "$base/releases" "$release/source"
tar -xf "$bundle" -C "$release/source"
"$python" -m venv "$release/venv"
"$release/venv/bin/python" -m pip install --only-binary=:all: -r "$release/source/requirements/alpaca-paper-linux-py313.lock.txt"
"$release/venv/bin/python" -m pip install --no-deps "$release/source"
"$release/venv/bin/python" -m pip check
"$release/venv/bin/python" -m pip freeze > "$release/dependencies.txt"
printf '%s\n' "$revision" > "$release/revision"
printf '#!/bin/sh\nexec %s/venv/bin/python -m factorlab.integrations.alpaca.cloud_runtime run --revision %s\n' "$release" "$revision" > "$release/run"
chmod 755 "$release/run"
install -m 644 "$release/source/deploy/systemd/"factorlab-alpaca-paper* /etc/systemd/system/
# Runtime state is never part of a release or rollback.
if [ -L "$base/current" ]; then
    readlink "$base/current" > "$base/previous-release"
    was_active=false
    if systemctl is-active --quiet factorlab-alpaca-paper.service; then was_active=true; fi
    state_root=$(systemctl show factorlab-alpaca-paper.service --property=Environment --value |
        "$base/current/venv/bin/python" -I -c 'import shlex, sys; env = dict(item.split("=", 1) for item in shlex.split(sys.stdin.read())); print(env.get("FACTORLAB_ALPACA_STATE_ROOT", "/var/lib/factorlab-alpaca-paper"))')
    systemctl stop factorlab-alpaca-paper.service
    if ! runuser -u factorlab-alpaca -- "$release/venv/bin/python" -I \
        -m factorlab.strategies.runtime_migration --market alpaca \
        --state-root "$state_root" \
        --previous-python "$base/current/venv/bin/python"; then
        if "$was_active"; then systemctl start factorlab-alpaca-paper.service; fi
        exit 1
    fi
fi
ln -s "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl daemon-reload
systemctl enable factorlab-alpaca-paper.service factorlab-alpaca-paper-health.timer
if [ -f /etc/factorlab-alpaca-paper/paper-secret.json ]; then
    systemctl start factorlab-alpaca-paper.service factorlab-alpaca-paper-health.timer
else
    printf 'Installed; waiting for Paper credential injection\n'
fi
