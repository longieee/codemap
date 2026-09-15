# discovery/gcp/

GCP topology discovery. `cr-topology.sh` snapshots one cloud project into a JSON document:
Cloud Run services and revisions, their env/secret wiring, Pub/Sub subscriptions, Cloud Scheduler
jobs and the service accounts that tie them together.

Read-only by construction (`gcloud ... describe` / `list` only) and one project per run — an
environment is its own map, and two environments are compared afterwards with
`stitcher/envdiff.py` rather than merged. Usage and the dimensions it widens over are documented
in `docs/cloud-discovery.md`; the output feeds `stitcher/reconcile.py --live`.
