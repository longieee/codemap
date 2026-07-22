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

G(){ gcloud "$@" --project "$PROJECT" --format=json 2>/dev/null || echo '[]'; }

# Each dimension → a read-only list/describe. `|| echo []` keeps a missing API from aborting the sweep.
COMPUTE_SERVICES="$(G run services list)"
COMPUTE_JOBS="$(G run jobs list)"
FUNCTIONS="$(G functions list)"
INSTANCES="$(G compute instances list)"

# invoke / IAM — per Cloud Run service (who may invoke it). Iterate service names.
INVOKE='[]'
for svc in $(echo "$COMPUTE_SERVICES" | python3 -c 'import sys,json;[print(s["metadata"]["name"]) for s in json.load(sys.stdin)]' 2>/dev/null); do
  pol="$(gcloud run services get-iam-policy "$svc" --project "$PROJECT" --region "${REGIONS%%,*}" --format=json 2>/dev/null || echo '{}')"
  INVOKE="$(python3 -c 'import sys,json;a=json.loads(sys.argv[1]);a.append({"service":sys.argv[2],"policy":json.loads(sys.argv[3])});print(json.dumps(a))' "$INVOKE" "$svc" "$pol")"
done
SERVICE_ACCOUNTS="$(G iam service-accounts list)"

# networking
VPCS="$(G compute networks list)"
SUBNETS="$(G compute networks subnets list)"
CONNECTORS="$(gcloud compute networks vpc-access connectors list --project "$PROJECT" --region "${REGIONS%%,*}" --format=json 2>/dev/null || echo '[]')"
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
REDIS="$(gcloud redis instances list --project "$PROJECT" --region "${REGIONS%%,*}" --format=json 2>/dev/null || echo '[]')"
BUCKETS="$(G storage buckets list)"

# secrets — NAMES ONLY, never versions access
SECRET_REFS="$(G secrets list)"

# scheduling
SCHEDULER="$(G scheduler jobs list)"
WORKFLOWS="$(G workflows list)"

python3 - "$PROJECT" <<PY
import json, sys
def L(s):
    try: return json.loads(s)
    except Exception: return []
doc = {
  "provider": "gcp", "project": sys.argv[1], "captured": "TIER_B_LIVE",
  "compute": {"services": L('''$COMPUTE_SERVICES'''), "jobs": L('''$COMPUTE_JOBS'''),
              "functions": L('''$FUNCTIONS'''), "instances": L('''$INSTANCES''')},
  "identity_invoke": {"invoke_policies": L('''$INVOKE'''), "service_accounts": L('''$SERVICE_ACCOUNTS''')},
  "networking": {"vpcs": L('''$VPCS'''), "subnets": L('''$SUBNETS'''), "connectors": L('''$CONNECTORS'''),
                 "firewalls": L('''$FIREWALLS'''), "routers": L('''$ROUTERS'''), "addresses": L('''$NAT_ADDRS''')},
  "dns_domains": {"zones": L('''$DNS_ZONES'''), "domain_mappings": L('''$DOMAIN_MAPPINGS''')},
  "load_balancing": {"url_maps": L('''$URL_MAPS'''), "backend_services": L('''$BACKEND_SERVICES'''),
                     "forwarding_rules": L('''$FORWARDING_RULES'''), "api_gateways": L('''$API_GATEWAYS''')},
  "certs": {"ssl_certificates": L('''$SSL_CERTS''')},
  "messaging": {"topics": L('''$TOPICS'''), "subscriptions": L('''$SUBSCRIPTIONS'''), "eventarc": L('''$EVENTARC''')},
  "datastores": {"sql": L('''$SQL'''), "redis": L('''$REDIS'''), "buckets": L('''$BUCKETS''')},
  "secrets_refs": L('''$SECRET_REFS'''),   # names only
  "scheduling": {"scheduler": L('''$SCHEDULER'''), "workflows": L('''$WORKFLOWS''')},
}
print(json.dumps(doc, indent=2))
PY
