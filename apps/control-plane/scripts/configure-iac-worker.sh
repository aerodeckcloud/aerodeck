#!/usr/bin/env bash
# Configure (or toggle) the tenant IaC Terraform engine on the Celery worker
# and persist the configuration in /etc/aerodeck/control-plane.env.
#
# Usage:
#   sudo bash configure-iac-worker.sh [--enable|--disable] [--apply|--no-apply]
#     [--spaces-access-id ID] [--spaces-secret-key SECRET] [--do-token TOKEN]
#
# Positional convenience:
#   sudo bash configure-iac-worker.sh <spaces-access-id> <spaces-secret-key> [--apply]
#
# Or pass credentials via environment variables:
#   SPACES_ACCESS_KEY_ID=... SPACES_SECRET_ACCESS_KEY=... sudo -E bash configure-iac-worker.sh
#
# Secrets are never printed to stdout or stderr.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root on the Droplet" >&2
  exit 1
fi

ENV_FILE="/etc/aerodeck/control-plane.env"
IMAGE_FILE="/etc/aerodeck/control-plane-image"

CORE_ENV_KEYS=(AERODECK_ENV AERODECK_AUTH_SIGNING_KEY AERODECK_DATABASE_URL AERODECK_REDIS_URL)
OPTIONAL_ENV_KEYS=(
  AERODECK_AUTH_CLIENTS
  AERODECK_TRAEFIK_PROVIDER_TOKEN
  AERODECK_DIGITALOCEAN_TOKEN
  AERODECK_DNS_RECORDS_PROVIDER
  AERODECK_DNS_RECORD_TYPE
  AERODECK_DNS_RECORD_TARGET
  AERODECK_DNS_RECORD_TTL
  AERODECK_DNS01_PROVIDER
  AERODECK_ACME_ISSUER
  AERODECK_ACME_DIRECTORY_URL
  AERODECK_ACME_ACCOUNT_EMAIL
  AERODECK_ACME_ACCOUNT_KEY
  AERODECK_PROVISIONER_BACKEND
  AERODECK_DOCKER_HOST
  AERODECK_DOCKER_RUNTIME
  AERODECK_DOCKER_ALLOWED_OWNERS
  AERODECK_DOCKER_BIND_HOST
  AERODECK_DOCKER_CONTAINER_PORT
  AERODECK_DOCKER_ROUTE_BASE_DOMAIN
  AERODECK_DOCKER_HEALTH_TIMEOUT_SECONDS
  AERODECK_IAC_WORKER
  AERODECK_IAC_APPLY
  AERODECK_SPACES_ACCESS_ID
  AERODECK_SPACES_SECRET_KEY
)

# Load existing environment if present
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

ENABLE="${AERODECK_IAC_WORKER:-true}"
APPLY="${AERODECK_IAC_APPLY:-true}"
SPACES_ACCESS_ID="${AERODECK_SPACES_ACCESS_ID:-${SPACES_ACCESS_KEY_ID:-}}"
SPACES_SECRET_KEY="${AERODECK_SPACES_SECRET_KEY:-${SPACES_SECRET_ACCESS_KEY:-}}"
DO_TOKEN="${AERODECK_DIGITALOCEAN_TOKEN:-${DIGITALOCEAN_TOKEN:-}}"
RESTART_WORKER="true"

# Handle positional arguments if first argument does not start with dash
if [[ $# -ge 2 && "$1" != -* && "$2" != -* ]]; then
  SPACES_ACCESS_ID="$1"
  SPACES_SECRET_KEY="$2"
  shift 2
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable)
      ENABLE="true"
      shift
      ;;
    --disable)
      ENABLE="false"
      shift
      ;;
    --enable=*)
      ENABLE="${1#*=}"
      shift
      ;;
    --apply)
      APPLY="true"
      shift
      ;;
    --no-apply|--plan-only)
      APPLY="false"
      shift
      ;;
    --apply=*)
      APPLY="${1#*=}"
      shift
      ;;
    --spaces-access-id)
      SPACES_ACCESS_ID="$2"
      shift 2
      ;;
    --spaces-access-id=*)
      SPACES_ACCESS_ID="${1#*=}"
      shift
      ;;
    --spaces-secret-key)
      SPACES_SECRET_KEY="$2"
      shift 2
      ;;
    --spaces-secret-key=*)
      SPACES_SECRET_KEY="${1#*=}"
      shift
      ;;
    --do-token)
      DO_TOKEN="$2"
      shift 2
      ;;
    --do-token=*)
      DO_TOKEN="${1#*=}"
      shift
      ;;
    --backend)
      AERODECK_PROVISIONER_BACKEND="$2"
      shift 2
      ;;
    --backend=*)
      AERODECK_PROVISIONER_BACKEND="${1#*=}"
      shift
      ;;
    --docker-container-port)
      AERODECK_DOCKER_CONTAINER_PORT="$2"
      shift 2
      ;;
    --docker-container-port=*)
      AERODECK_DOCKER_CONTAINER_PORT="${1#*=}"
      shift
      ;;
    --no-restart)
      RESTART_WORKER="false"
      shift
      ;;
    --help|-h)
      echo "usage: $0 [--enable|--disable] [--apply|--no-apply] [--backend docker|terraform|noop] [--docker-container-port PORT] [--spaces-access-id ID] [--spaces-secret-key SECRET] [--do-token TOKEN] [--no-restart]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "${ENABLE}" == "true" ]]; then
  if [[ -z "${SPACES_ACCESS_ID}" || -z "${SPACES_SECRET_KEY}" ]]; then
    echo "error: SPACES_ACCESS_KEY_ID and SPACES_SECRET_ACCESS_KEY are required to enable IaC worker" >&2
    exit 2
  fi
  if [[ -z "${DO_TOKEN}" ]]; then
    echo "error: DIGITALOCEAN_TOKEN is required to enable IaC worker" >&2
    exit 2
  fi
fi

AERODECK_PROVISIONER_BACKEND="${AERODECK_PROVISIONER_BACKEND:-docker}"
# DigitalOcean eth0 carries a 10.18.x.x anchor/metadata IP; the VPC interface is eth1 (e.g. 10.41.0.2).
if [[ -z "${AERODECK_DOCKER_BIND_HOST:-}" || "${AERODECK_DOCKER_BIND_HOST}" == 10.18.* ]]; then
  PRIVATE_VPC_IP="$(ip -4 addr show dev eth1 2>/dev/null | awk '/inet 10\./ {sub(/\/.*/, "", $2); print $2; exit}' || true)"
  if [[ -z "${PRIVATE_VPC_IP}" ]]; then
    PRIVATE_VPC_IP="$(ip -4 addr show 2>/dev/null | awk '/inet 10\./ && $2 !~ /^10\.18\./ {sub(/\/.*/, "", $2); print $2; exit}' || true)"
  fi
  AERODECK_DOCKER_BIND_HOST="${PRIVATE_VPC_IP:-127.0.0.1}"
fi
AERODECK_DOCKER_CONTAINER_PORT="${AERODECK_DOCKER_CONTAINER_PORT:-8000}"
# New apps publish on the product zone. Move only the previous dev default.
if [[ -z "${AERODECK_DOCKER_ROUTE_BASE_DOMAIN:-}" || "${AERODECK_DOCKER_ROUTE_BASE_DOMAIN}" == "dev.app.aero-deck.app" ]]; then
  AERODECK_DOCKER_ROUTE_BASE_DOMAIN="dev.aerodeckcloud.com"
fi
if [[ -z "${AERODECK_DNS_RECORD_TARGET:-}" || "${AERODECK_DNS_RECORD_TARGET}" == "edge.aero-deck.app" ]]; then
  AERODECK_DNS_RECORD_TARGET="edge.aerodeckcloud.com"
fi
AERODECK_DOCKER_HEALTH_TIMEOUT_SECONDS="${AERODECK_DOCKER_HEALTH_TIMEOUT_SECONDS:-30}"
AERODECK_IAC_WORKER="${ENABLE}"
AERODECK_IAC_APPLY="${APPLY}"
if [[ -n "${SPACES_ACCESS_ID}" ]]; then
  AERODECK_SPACES_ACCESS_ID="${SPACES_ACCESS_ID}"
fi
if [[ -n "${SPACES_SECRET_KEY}" ]]; then
  AERODECK_SPACES_SECRET_KEY="${SPACES_SECRET_KEY}"
fi
if [[ -n "${DO_TOKEN}" ]]; then
  AERODECK_DIGITALOCEAN_TOKEN="${DO_TOKEN}"
fi

mkdir -p /etc/aerodeck
umask 077
{
  printf 'AERODECK_ENV=%q\n' "${AERODECK_ENV:-dev}"
  printf 'AERODECK_AUTH_SIGNING_KEY=%q\n' "${AERODECK_AUTH_SIGNING_KEY:-}"
  printf 'AERODECK_DATABASE_URL=%q\n' "${AERODECK_DATABASE_URL:-}"
  printf 'AERODECK_REDIS_URL=%q\n' "${AERODECK_REDIS_URL:-redis://127.0.0.1:6379/0}"
  for key in "${OPTIONAL_ENV_KEYS[@]}"; do
    val="${!key:-}"
    if [[ -n "${val}" ]]; then
      printf '%s=%q\n' "${key}" "${val}"
    fi
  done
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# Unset secrets from shell memory
unset SPACES_SECRET_KEY DO_TOKEN

echo "persisted IaC configuration in ${ENV_FILE}"

if [[ "${RESTART_WORKER}" == "true" ]]; then
  if [[ -f "${IMAGE_FILE}" ]]; then
    IMAGE="$(tr -d '[:space:]' < "${IMAGE_FILE}")"
  elif docker inspect -f '{{.Config.Image}}' aerodeck-worker >/dev/null 2>&1; then
    IMAGE="$(docker inspect -f '{{.Config.Image}}' aerodeck-worker)"
  else
    IMAGE="registry.digitalocean.com/aerodeck/control-plane:main"
  fi

  DOCKER_ENV_FLAGS=()
  for key in "${CORE_ENV_KEYS[@]}" "${OPTIONAL_ENV_KEYS[@]}"; do
    if [[ -n "${!key:-}" ]]; then
      DOCKER_ENV_FLAGS+=(-e "${key}")
    fi
  done

  WORKER_EXTRA_FLAGS=()
  if [[ "${AERODECK_PROVISIONER_BACKEND:-noop}" == "docker" && -z "${AERODECK_DOCKER_HOST:-}" && -S /var/run/docker.sock ]]; then
    DOCKER_SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
    WORKER_EXTRA_FLAGS+=(-v /var/run/docker.sock:/var/run/docker.sock --group-add "${DOCKER_SOCK_GID}")
  fi

  CERT_KEY_DIR=/var/lib/aerodeck/certs
  mkdir -p "${CERT_KEY_DIR}"
  chown 10001:10001 "${CERT_KEY_DIR}"
  chmod 700 "${CERT_KEY_DIR}"

  echo "restarting aerodeck-worker container with ${IMAGE}..."
  docker rm -f aerodeck-worker >/dev/null 2>&1 || true
  docker run -d --name aerodeck-worker --network host --restart unless-stopped \
    -v "${CERT_KEY_DIR}:${CERT_KEY_DIR}" \
    "${WORKER_EXTRA_FLAGS[@]}" \
    "${DOCKER_ENV_FLAGS[@]}" \
    "${IMAGE}" \
    celery -A app.worker worker --beat --queues aerodeck --loglevel INFO

  sleep 2
  if ! docker inspect -f '{{.State.Running}}' aerodeck-worker >/dev/null 2>&1; then
    echo "warning: aerodeck-worker is not in running state" >&2
    docker logs --tail 30 aerodeck-worker >&2 || true
    exit 1
  fi
  echo "aerodeck-worker restarted successfully"
fi

echo "IaC worker configuration complete (enabled=${AERODECK_IAC_WORKER}, apply=${AERODECK_IAC_APPLY})"

