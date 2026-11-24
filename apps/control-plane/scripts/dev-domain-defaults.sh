# Product-zone defaults for new apps. Sourced by start-on-droplet.sh and
# configure-iac-worker.sh. Replaces only the previous dev literals. A custom
# value is left alone. Stored route hostnames are not rewritten here.
apply_dev_domain_defaults() {
  if [[ -z "${AERODECK_DOCKER_ROUTE_BASE_DOMAIN:-}" || "${AERODECK_DOCKER_ROUTE_BASE_DOMAIN}" == "dev.app.aero-deck.app" ]]; then
    AERODECK_DOCKER_ROUTE_BASE_DOMAIN="dev.aerodeckcloud.com"
  fi
  if [[ -z "${AERODECK_DNS_RECORD_TARGET:-}" || "${AERODECK_DNS_RECORD_TARGET}" == "edge.aero-deck.app" ]]; then
    AERODECK_DNS_RECORD_TARGET="edge.aerodeckcloud.com"
  fi
}
