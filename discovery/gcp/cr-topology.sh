#!/usr/bin/env bash
# codemap — GCP live topology discovery (Tier B). READ-ONLY.
# Emits one JSON doc covering every cloud-discovery dimension (see docs/cloud-discovery.md §2/§4),
# so stitcher/reconcile.py can join it against the static IaC (infra.py).
#
# Usage:   discovery/gcp/cr-topology.sh --project PROJECT [--regions r1,r2] > topology.json
# Auth:    requires `gcloud auth` already set up; fails closed with a clear message otherwise.
# Safety:  ONLY list/describe/get-iam-policy. Never mutates. Secret VALUES never read (names only).
set -euo pipefail

PROJECT=""; REGIONS=""
while [ $# -gt 0 ]; do case "$1" in
  --project) PROJECT="$2"; shift 2;;
  --regions) REGIONS="$2"; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done

command -v gcloud >/dev/null || { echo '{"error":"gcloud not installed"}'; exit 1; }
[ -n "$PROJECT" ] || PROJECT="$(gcloud config get-value project 2>/dev/null)"
gcloud auth print-access-token >/dev/null 2>&1 || { echo '{"error":"not authenticated (run: gcloud auth login)"}'; exit 1; }

# Every call is bounded (CR_CALL_TIMEOUT, default 60s) so one slow/hanging API can't stall the
# whole sweep — a timed-out dimension degrades to [] rather than blocking discovery.
G(){ timeout "${CR_CALL_TIMEOUT:-60}" gcloud "$@" --project "$PROJECT" --format=json 2>/dev/null || echo '[]'; }

# Each dimension → a read-only list/describe. `|| echo []` keeps a missing API from aborting the sweep.
COMPUTE_SERVICES="$(G run services list)"
COMPUTE_JOBS="$(G run jobs list)"
FUNCTIONS="$(G functions list)"
INSTANCES="$(G compute instances list)"

# invoke / IAM — per Cloud Run service (who may invoke it). get-iam-policy is REGION-SCOPED, so it
# only works when a region is known; without --regions it would error once per service (100s of
# wasted calls on a large project). Guard on a region + bound each call.
INVOKE='[]'
REGION1="${REGIONS%%,*}"
if [ -n "$REGION1" ]; then
  for svc in $(echo "$COMPUTE_SERVICES" | python3 -c 'import sys,json;[print(s["metadata"]["name"]) for s in json.load(sys.stdin)]' 2>/dev/null); do
    pol="$(timeout 20 gcloud run services get-iam-policy "$svc" --project "$PROJECT" --region "$REGION1" --format=json 2>/dev/null || echo '{}')"
    INVOKE="$(python3 -c 'import sys,json;a=json.loads(sys.argv[1]);a.append({"service":sys.argv[2],"policy":json.loads(sys.argv[3])});print(json.dumps(a))' "$INVOKE" "$svc" "$pol")"
  done
else
  echo "cr-topology: invoke policies skipped (get-iam-policy is region-scoped; pass --regions to include them)" >&2
fi
SERVICE_ACCOUNTS="$(G iam service-accounts list)"

# networking
VPCS="$(G compute networks list)"
SUBNETS="$(G compute networks subnets list)"
CONNECTORS="$(timeout "${CR_CALL_TIMEOUT:-60}" gcloud compute networks vpc-access connectors list --project "$PROJECT" --region "${REGIONS%%,*}" --format=json 2>/dev/null || echo '[]')"
FIREWALLS="$(G compute firewall-rules list)"
ROUTERS="$(G compute routers list)"
NAT_ADDRS="$(G compute addresses list)"

# DNS & domains  ⟵ previously missing
DNS_ZONES="$(G dns managed-zones list)"
DOMAIN_MAPPINGS="$(G run domain-mappings list)"

# load balancing & edge
URL_MAPS="$(G compute url-maps list)"
BACKEND_SERVICES="$(G compute backend-services list)"
FORWARDING_RULES="$(G compute forwarding-rules list)"
API_GATEWAYS="$(G api-gateway gateways list)"

# certs
SSL_CERTS="$(G compute ssl-certificates list)"

# messaging
TOPICS="$(G pubsub topics list)"
SUBSCRIPTIONS="$(G pubsub subscriptions list)"
EVENTARC="$(G eventarc triggers list)"

# data stores
SQL="$(G sql instances list)"
REDIS="$(timeout "${CR_CALL_TIMEOUT:-60}" gcloud redis instances list --project "$PROJECT" --region "${REGIONS%%,*}" --format=json 2>/dev/null || echo '[]')"
BUCKETS="$(G storage buckets list)"

# secrets — NAMES ONLY, never versions access
SECRET_REFS="$(G secrets list)"

# scheduling
SCHEDULER="$(G scheduler jobs list)"
WORKFLOWS="$(G workflows list)"

# Assemble via TEMP FILES, not heredoc interpolation. Embedding captured JSON into a Python string
# literal (`L('''$VAR''')`) is unsafe: bash interpolates it into a NON-raw string, so backslash
# sequences in the data (e.g. in a service annotation) get interpreted by Python and corrupt the
# JSON → json.loads fails → the whole dimension is silently dropped. Writing each var to a file and
# json.load-ing it (with a QUOTED heredoc, no interpolation) preserves the bytes exactly.
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
w(){ printf '%s' "$2" > "$TMPD/$1.json"; }
w compute_services "$COMPUTE_SERVICES";   w compute_jobs "$COMPUTE_JOBS"
w functions "$FUNCTIONS";                 w instances "$INSTANCES"
w invoke "$INVOKE";                       w service_accounts "$SERVICE_ACCOUNTS"
w vpcs "$VPCS";                           w subnets "$SUBNETS";        w connectors "$CONNECTORS"
w firewalls "$FIREWALLS";                 w routers "$ROUTERS";        w nat_addrs "$NAT_ADDRS"
w dns_zones "$DNS_ZONES";                 w domain_mappings "$DOMAIN_MAPPINGS"
w url_maps "$URL_MAPS";                   w backend_services "$BACKEND_SERVICES"
w forwarding_rules "$FORWARDING_RULES";   w api_gateways "$API_GATEWAYS"
w ssl_certs "$SSL_CERTS"
w topics "$TOPICS";                       w subscriptions "$SUBSCRIPTIONS";  w eventarc "$EVENTARC"
w sql "$SQL";                             w redis "$REDIS";            w buckets "$BUCKETS"
w secret_refs "$SECRET_REFS"
w scheduler "$SCHEDULER";                 w workflows "$WORKFLOWS"

python3 - "$PROJECT" "$TMPD" <<'PY'
import json, sys, os
PROJECT, D = sys.argv[1], sys.argv[2]
def L(name):
    try: return json.load(open(os.path.join(D, name + ".json")))
    except Exception: return []
doc = {
  "provider": "gcp", "project": PROJECT, "captured": "TIER_B_LIVE",
  "compute": {"services": L("compute_services"), "jobs": L("compute_jobs"),
              "functions": L("functions"), "instances": L("instances")},
  "identity_invoke": {"invoke_policies": L("invoke"), "service_accounts": L("service_accounts")},
  "networking": {"vpcs": L("vpcs"), "subnets": L("subnets"), "connectors": L("connectors"),
                 "firewalls": L("firewalls"), "routers": L("routers"), "addresses": L("nat_addrs")},
  "dns_domains": {"zones": L("dns_zones"), "domain_mappings": L("domain_mappings")},
  "load_balancing": {"url_maps": L("url_maps"), "backend_services": L("backend_services"),
                     "forwarding_rules": L("forwarding_rules"), "api_gateways": L("api_gateways")},
  "certs": {"ssl_certificates": L("ssl_certs")},
  "messaging": {"topics": L("topics"), "subscriptions": L("subscriptions"), "eventarc": L("eventarc")},
  "datastores": {"sql": L("sql"), "redis": L("redis"), "buckets": L("buckets")},
  "secrets_refs": L("secret_refs"),   # names only
  "scheduling": {"scheduler": L("scheduler"), "workflows": L("workflows")},
}
print(json.dumps(doc, indent=2))
PY
