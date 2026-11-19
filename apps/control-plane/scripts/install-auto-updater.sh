#!/usr/bin/env bash
# Install and enable the pull-based auto-updater systemd timer on the control Droplet (#213).
# Eliminates the need for a self-hosted GitHub Actions runner or inbound CI SSH.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root on the Droplet" >&2
  exit 1
fi

SCRIPT_DIR="/root/aerodeck"
RECONCILER="${SCRIPT_DIR}/reconcile-droplet-images.sh"

if [[ ! -f "${RECONCILER}" ]]; then
  echo "missing ${RECONCILER}; copy scripts to ${SCRIPT_DIR} first" >&2
  exit 1
fi

chmod 700 "${RECONCILER}"

cat >/etc/systemd/system/aerodeck-auto-updater.service <<'UNIT'
[Unit]
Description=AeroDeck pull-based DOCR image reconciler
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash /root/aerodeck/reconcile-droplet-images.sh
StandardOutput=journal
StandardError=journal
UNIT

cat >/etc/systemd/system/aerodeck-auto-updater.timer <<'UNIT'
[Unit]
Description=Poll DOCR every 60s for new AeroDeck images

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now aerodeck-auto-updater.timer
systemctl status aerodeck-auto-updater.timer --no-pager
