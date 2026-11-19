#!/usr/bin/env bash
# Pull-based image reconciler for the AeroDeck control Droplet (#213).
#
# Triggered periodically by aerodeck-auto-updater.timer (every 60s).
# Checks DOCR for newer digests on :main and :dashboard-main, restarts only
# when a digest changes, verifies health endpoints, and rolls back to the
# previous image ID if the new image fails readiness checks.
#
# Secrets are never printed to stdout or stderr.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root on the Droplet" >&2
  exit 1
fi

LOCK_FILE="/run/aerodeck-reconcile.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  exit 0
fi

ENV_FILE="/etc/aerodeck/control-plane.env"
SCRIPT_DIR="/root/aerodeck"
CP_IMAGE="registry.digitalocean.com/aerodeck/control-plane:main"
DASH_IMAGE="registry.digitalocean.com/aerodeck/control-plane:dashboard-main"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

TOKEN="${AERODECK_DIGITALOCEAN_TOKEN:-}"
if [[ -z "${TOKEN}" ]]; then
  echo "AERODECK_DIGITALOCEAN_TOKEN not set in ${ENV_FILE}; skipping reconcile" >&2
  exit 0
fi

printf '%s' "${TOKEN}" | docker login registry.digitalocean.com --username "${TOKEN}" --password-stdin >/dev/null 2>&1
unset TOKEN

image_id_for() {
  docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}

reconcile_control_plane() {
  local prev_id new_id
  prev_id="$(image_id_for "${CP_IMAGE}")"
  if ! docker pull --quiet "${CP_IMAGE}" >/dev/null 2>&1; then
    echo "warning: failed to pull ${CP_IMAGE}" >&2
    return 0
  fi
  new_id="$(image_id_for "${CP_IMAGE}")"
  if [[ -z "${new_id}" || "${new_id}" == "${prev_id}" ]]; then
    return 0
  fi

  echo "control-plane digest changed (${prev_id:-none} -> ${new_id}); restarting API + worker"
  if bash "${SCRIPT_DIR}/start-on-droplet.sh" && curl -fsS --max-time 5 http://127.0.0.1:8000/readyz >/dev/null; then
    echo "control-plane updated and ready"
    return 0
  fi

  echo "error: control-plane update failed health check; rolling back" >&2
  if [[ -n "${prev_id}" ]]; then
    docker tag "${prev_id}" "${CP_IMAGE}"
    bash "${SCRIPT_DIR}/start-on-droplet.sh" || true
  fi
  return 1
}

reconcile_dashboard() {
  local prev_id new_id
  prev_id="$(image_id_for "${DASH_IMAGE}")"
  if ! docker pull --quiet "${DASH_IMAGE}" >/dev/null 2>&1; then
    echo "warning: failed to pull ${DASH_IMAGE}" >&2
    return 0
  fi
  new_id="$(image_id_for "${DASH_IMAGE}")"
  if [[ -z "${new_id}" || "${new_id}" == "${prev_id}" ]]; then
    return 0
  fi

  echo "dashboard digest changed (${prev_id:-none} -> ${new_id}); restarting dashboard"
  if bash "${SCRIPT_DIR}/start-dashboard-on-droplet.sh" "${DASH_IMAGE}" && curl -fsS --max-time 5 http://127.0.0.1:3000/login >/dev/null; then
    echo "dashboard updated and ready"
    return 0
  fi

  echo "error: dashboard update failed health check; rolling back" >&2
  if [[ -n "${prev_id}" ]]; then
    docker tag "${prev_id}" "${DASH_IMAGE}"
    bash "${SCRIPT_DIR}/start-dashboard-on-droplet.sh" "${DASH_IMAGE}" || true
  fi
  return 1
}

reconcile_control_plane
reconcile_dashboard
