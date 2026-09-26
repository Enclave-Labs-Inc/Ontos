# Ontos Helm chart

Deploys Ontos into a customer's VPC. Defaults are tuned for regulated deploys:

- **Deny-egress NetworkPolicy** — the pod cannot phone home. Egress is explicitly whitelisted per-endpoint under `networkPolicy.allowedEgress`; the chart ships defaults for the storage backends the runtime supports (Neo4j, Postgres for audit, OpenFGA).
- **KMS-fed secrets** — no key material lives in `values.yaml`. `ONTOS_AUDIT_SIGNING_KEY`, `NEO4J_PASSWORD`, `ONTOS_AUDIT_DB_URL`, `FGA_API_TOKEN` all resolve from `Secret` references you populate externally (external-secrets-operator sourced from your KMS / Vault / customer key manager).
- **`readOnlyRootFilesystem: true`** — writes go only to explicitly-mounted `emptyDir`s (`/tmp`, `/var/lib/ontos/audit-logs`).
- **Non-root** — runs as UID 1000, matching the Dockerfile.
- **Resource limits** — 2 CPU / 4 Gi RAM by default. Override per tenant.
- **Single replica by default** — Ontos is stateful (Neo4j sessions + audit chain); scale horizontally only after wiring a session-affinity front door.

## Install

```bash
# Create the secrets first (the chart never accepts inline key material)
kubectl create secret generic ontos-audit-signing-key \
    --from-literal=signing-key="$(openssl rand -hex 32)"
kubectl create secret generic ontos-neo4j \
    --from-literal=user=neo4j \
    --from-literal=password="$(openssl rand -hex 24)"
kubectl create secret generic ontos-audit-db \
    --from-literal=url="postgresql+asyncpg://ontos:...@postgres:5432/ontos_audit"
# OpenFGA is optional — omit if ONTOS_AUTHZ_BACKEND=inmemory or none.
kubectl create secret generic ontos-openfga \
    --from-literal=token="..."

helm install ontos ./deploy/helm/ontos \
    --set image.tag=0.0.1
```

## Regulated-deploy checklist

Before pointing customer traffic at an Ontos deploy:

- [ ] `helm lint ./deploy/helm/ontos` clean.
- [ ] `helm template ... | kubectl apply --dry-run=server -f -` clean against the target cluster.
- [ ] Every `Secret` populated via external-secrets, NOT kubectl imperative (leaves the plaintext in shell history).
- [ ] `NetworkPolicy` present and enforced (some managed K8s distros ignore NetworkPolicy without a CNI that supports it — Calico / Cilium do; the default AWS VPC CNI does not until you install a policy engine).
- [ ] `ONTOS_ENV=prod` — the runtime refuses to boot without a real signing key when this is set.
- [ ] Audit `ONTOS_AUDIT_DB_URL` uses an async driver (`postgresql+asyncpg://`); the runtime hard-fails on sync drivers.
- [ ] Backup + retention policy for the Postgres audit DB matches the customer's regulatory obligation (Article 12 = ≥6 months for high-risk AI systems).

## What's NOT in the chart

- Neo4j itself — bring your own cluster (or use Neo4j's operator).
- Postgres — same. The chart references your Secrets; provisioning storage is out of scope.
- OpenFGA — same.
- Ingress — deliberately not shipped; every regulated deploy fronts the MCP endpoint with its own gateway (Envoy / Istio / Traefik with the customer's mTLS + logging story).

Each of the above is intentionally the customer's decision. The chart deploys just the Ontos runtime and its explicit egress rules.
