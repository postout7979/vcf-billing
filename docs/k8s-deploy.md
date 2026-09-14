# VCF Billing Portal — Kubernetes Deployment Guide

This is an alternative to the Docker Compose deployment (`docs/docker-deploy.md`)
for anyone running a Kubernetes cluster instead of a single Ubuntu server. It
deploys the exact same five components as plain YAML manifests under `k8s/` —
no Helm or Kustomize required.

## Who this is for

Anyone who wants to run the VCF Billing Portal on an existing Kubernetes
cluster (on-prem, VCF-based, or otherwise) rather than a single Docker
Compose host. If you don't already have a cluster, use
`docs/docker-deploy.md` instead — it's simpler for a single server.

## What you get

| Manifest              | Kubernetes objects                          | Role                                                             |
|------------------------|----------------------------------------------|--------------------------------------------------------------------|
| `00-namespace.yaml`     | Namespace                                     | Dedicated `vcf-billing` namespace for everything below            |
| `01-secret.example.yaml`| Secret (template)                             | `SECRET_KEY`, PostgreSQL credentials, `DATABASE_URL`               |
| `02-configmap.yaml`     | ConfigMap                                     | Non-sensitive settings (collector interval, token TTL, ...)        |
| `03-postgres.yaml`      | Service (headless) + StatefulSet + PVC        | PostgreSQL 16 — persistent data store                              |
| `04-migrate-job.yaml`   | Job                                           | One-shot: creates the schema and the default admin account         |
| `05-api.yaml`           | Service (ClusterIP) + Deployment              | FastAPI app (billing, admin API, PDF statements)                   |
| `06-collector.yaml`     | Deployment                                    | Background loop that polls VCF Operations every N minutes          |
| `07-frontend.yaml`      | ConfigMap + Deployment + Service (LoadBalancer)| Nginx — serves the static UI, reverse-proxies `/api/*`, external entry point |

Only PostgreSQL's data (the `data` PersistentVolumeClaim created by the
StatefulSet) is state that must survive a redeploy. `api`, `collector`, and
`frontend` are stateless and can each be rebuilt/rolled independently, same
as the Docker Compose deployment.

## Prerequisites

- A Kubernetes cluster (1.26+) and `kubectl` configured against it, with
  permission to create namespaces.
- A container registry reachable from both your build machine and the
  cluster's nodes (an internal/private registry is fine — nothing here
  assumes Docker Hub).
- Docker (or another OCI builder) to build the three application images from
  the existing `docker/api`, `docker/collector`, `docker/frontend`
  Dockerfiles — these are unchanged from the Docker Compose deployment.
- A way to reach the cluster's assigned LoadBalancer IP from your internal
  network (cloud LB, MetalLB, or your organization's own layer).

## 0. What's different from the Docker Compose deployment

- **Same five components, different orchestration.** The application code,
  Dockerfiles, and env vars are all unchanged — only how the containers are
  scheduled, networked, and exposed differs.
- **`frontend`'s nginx config is replaced, not the image.** The Docker image
  (`docker/frontend/Dockerfile`) bakes in a config that re-resolves the
  `api` hostname via Docker's embedded DNS on every request, because a
  redeployed Docker container gets a new IP. A Kubernetes `Service` doesn't
  have that problem — its ClusterIP is stable for the Service's lifetime, so
  `07-frontend.yaml` mounts a plain `proxy_pass http://vcf-billing-api:8000;`
  config via a ConfigMap, overriding the image's built-in one. The frontend
  *image* itself is identical to the Docker Compose deployment.
- **`migrate` is a Job, not a `depends_on: condition:`.** Docker Compose can
  block `api`/`collector` from starting until `migrate` exits successfully;
  plain Kubernetes manifests can't express that dependency declaratively.
  Instead, the deploy steps below run the Job and explicitly `kubectl wait`
  for it to complete before applying the other Deployments.
- **`collector` must stay at `replicas: 1`.** This was already true in
  Docker Compose (one `collector` container), but Kubernetes makes it easy
  to accidentally scale a Deployment — `06-collector.yaml` calls this out
  explicitly and uses `strategy: Recreate` so a rolling update never briefly
  runs two collector pods at once.
- **TLS termination is still out of scope**, exactly as in the Docker
  Compose deployment (where the host's nginx + certbot handled it in front
  of the `frontend` container). Here, put your own reverse proxy / L7 load
  balancer in front of the `vcf-billing-frontend` Service's external IP and
  terminate TLS there.

## 1. Build and push the images

```bash
cd /path/to/vcf-billing-portal   # repository root — same context docker-compose.yml uses

export REGISTRY=registry.internal.example.com/vcf-billing   # your registry
export TAG=v4.11.0                                            # any tag you like

docker build -t "$REGISTRY/vcf-billing-api:$TAG"       -f docker/api/Dockerfile .
docker build -t "$REGISTRY/vcf-billing-collector:$TAG" -f docker/collector/Dockerfile .
docker build -t "$REGISTRY/vcf-billing-frontend:$TAG"  -f docker/frontend/Dockerfile .

docker push "$REGISTRY/vcf-billing-api:$TAG"
docker push "$REGISTRY/vcf-billing-collector:$TAG"
docker push "$REGISTRY/vcf-billing-frontend:$TAG"
```

If your cluster's nodes can't reach the registry you pushed to (air-gapped
environments), transfer the images with `docker save` / `docker load` (or
your registry's own mirroring tool) instead — the manifests only care that
`$REGISTRY/vcf-billing-*:$TAG` is pullable from inside the cluster.

If the registry requires authentication, create a pull secret and reference
it from each Deployment/Job (`imagePullSecrets:` under `spec.template.spec`
in `04-migrate-job.yaml`, `05-api.yaml`, `06-collector.yaml`,
`07-frontend.yaml`):

```bash
kubectl create secret docker-registry vcf-billing-regcred \
  --namespace vcf-billing \
  --docker-server="$REGISTRY" --docker-username=<user> --docker-password=<password>
```

## 2. Fill in the image placeholders

Every manifest that runs application code has `<YOUR_REGISTRY>` and `<TAG>`
placeholders instead of a real image reference:

```bash
cd k8s
sed -i "s#<YOUR_REGISTRY>#${REGISTRY}#g; s#<TAG>#${TAG}#g" \
  04-migrate-job.yaml 05-api.yaml 06-collector.yaml 07-frontend.yaml
```

## 3. Configure the Secret

```bash
cp 01-secret.example.yaml 01-secret.yaml
nano 01-secret.yaml
```

At minimum, change (see the comments inside the file for details on every
field — this mirrors `.env.example` exactly):

- `SECRET_KEY` — a random value from `openssl rand -hex 32`. **Never change
  this once the service is running** — it also encrypts stored
  integration-account credentials.
- `POSTGRES_PASSWORD` — replace with a strong password.
- `DATABASE_URL` — must match `POSTGRES_PASSWORD` above exactly (the
  password is embedded directly in the URL).

`k8s/01-secret.yaml` is already in `.gitignore` — only the `.example`
template is meant to be committed, same principle as `.env`/`.env.example`.

## 4. Deploy

```bash
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-secret.yaml
kubectl apply -f 02-configmap.yaml
kubectl apply -f 03-postgres.yaml

# Wait for PostgreSQL to actually be ready before running the schema migration
kubectl wait --for=condition=ready pod -l app=vcf-billing-postgres -n vcf-billing --timeout=180s

kubectl apply -f 04-migrate-job.yaml
kubectl wait --for=condition=complete job/vcf-billing-migrate -n vcf-billing --timeout=180s

kubectl apply -f 05-api.yaml
kubectl apply -f 06-collector.yaml
kubectl apply -f 07-frontend.yaml
```

Check everything came up:

```bash
kubectl get pods -n vcf-billing
kubectl get svc vcf-billing-frontend -n vcf-billing   # note the EXTERNAL-IP once assigned
```

`vcf-billing-migrate` should show `Completed` (it's a Job, not a long-running
pod — that's expected). `vcf-billing-postgres-0`, the `vcf-billing-api-*`,
`vcf-billing-collector-*`, and `vcf-billing-frontend-*` pods should all show
`Running` and `1/1` (or `2/2`, etc.) Ready.

```bash
kubectl port-forward -n vcf-billing svc/vcf-billing-frontend 8080:80 &
curl -s http://127.0.0.1:8080/api/health   # expect {"status":"ok"}
```

## 5. Expose it externally / TLS

`vcf-billing-frontend`'s Service is `type: LoadBalancer`, so your cluster's
load-balancer integration (a cloud provider, MetalLB, etc.) assigns it an
external IP automatically:

```bash
kubectl get svc vcf-billing-frontend -n vcf-billing
```

TLS termination is not handled by these manifests, the same way the Docker
Compose deployment leaves it to the host's nginx + certbot. Point your
existing reverse proxy, L7 load balancer, or an Ingress controller you
already run elsewhere at this Service's external IP on port 80, and
terminate HTTPS there.

Open the resulting URL, log in as `admin` / `admin1!2@3#`, and confirm the
dashboard renders — then immediately change the admin password. From here,
registering an integration account and creating tenants/projects follow the
same steps as README.md.

## 6. Redeploying a new version

```bash
export OLD_TAG=v4.11.0
export TAG=v4.12.0   # new tag you built and pushed

sed -i "s#:$OLD_TAG#:$TAG#g" 04-migrate-job.yaml 05-api.yaml 06-collector.yaml 07-frontend.yaml

# The migrate Job's pod template is immutable — delete and recreate it every
# time, even if the schema itself didn't change this release.
kubectl delete job/vcf-billing-migrate -n vcf-billing --ignore-not-found
kubectl apply -f 04-migrate-job.yaml
kubectl wait --for=condition=complete job/vcf-billing-migrate -n vcf-billing --timeout=180s

kubectl apply -f 05-api.yaml
kubectl apply -f 06-collector.yaml
kubectl apply -f 07-frontend.yaml
```

`vcf-billing-collector` uses `strategy: Recreate`, so there's a short gap
(usually well under a minute) where no collector pod is running during its
rollout — acceptable given the 5-minute collection cycle. `api` and
`frontend` roll normally (`RollingUpdate`, the Deployment default).

## 7. Scaling

- `vcf-billing-api`: safe to scale (`kubectl scale deployment/vcf-billing-api
  --replicas=3 -n vcf-billing`) — all state lives in PostgreSQL.
- `vcf-billing-frontend`: safe to scale freely — stateless static files +
  reverse proxy.
- `vcf-billing-collector`: **do not scale past 1 replica** — see
  `06-collector.yaml`'s comment for why (duplicate VM/power-sample
  collection).
- `vcf-billing-postgres`: this StatefulSet is a single instance with no
  built-in replication/HA, same limitation as the Docker Compose `db`
  service. For real HA, point `DATABASE_URL` in `01-secret.yaml` at an
  existing managed/clustered PostgreSQL instead of applying
  `03-postgres.yaml` at all.

## 8. Migrating an existing Docker Compose deployment's data

Two options, in order of preference:

**Option A — `pg_dump`/`pg_restore` (exact, includes integration-account
credentials):**

```bash
# On the old Docker Compose host
docker compose exec db pg_dump -U vcfbilling -Fc vcfbilling > vcfbilling.dump

# Copy vcfbilling.dump to somewhere kubectl can reach, then:
kubectl cp vcfbilling.dump vcf-billing/vcf-billing-postgres-0:/tmp/vcfbilling.dump
kubectl exec -n vcf-billing vcf-billing-postgres-0 -- \
  pg_restore -U vcfbilling -d vcfbilling --clean --if-exists /tmp/vcfbilling.dump
```

Run this **before** applying `04-migrate-job.yaml`/`05-api.yaml`/etc. (or
after tearing them down), so nothing is writing to the database mid-restore.
If Billing DB and Operations DB were split into separate physical databases
in the old deployment (see README's "Operations DB" section), repeat for
`vcfbilling_ops` as well.

**Option B — the app's own backup/restore feature (simpler, but excludes
integration-account connection info by design):** use "데이터베이스 → 백업 &
복구" in the admin UI on the old deployment to download a backup file, then
upload it through the same UI on the fresh Kubernetes deployment. Any
integration accounts will need their connection info re-entered afterward
(the admin UI flags them "⚠ 재등록 필요"). This is the same mechanism
described in the design doc's v4.10 section — it's meant for disaster
recovery within one deployment, but works equally well as a lighter-weight
cross-deployment migration when you don't need the connection credentials
to carry over automatically.

## Rolling back

- **A single component misbehaves after a redeploy**: re-point that
  manifest's `image:` tag at the previous version and re-apply just that
  file (`kubectl apply -f 05-api.yaml`).
- **The whole stack needs to roll back**: repeat step 6 with the previous
  tag for every manifest.
- **Database rollback**: same caveat as the Docker Compose deployment —
  nothing here backs up PostgreSQL automatically. Take your own `pg_dump`
  (or use the admin UI's backup feature, per step 8) before any change
  you're unsure about. Deleting the StatefulSet's PVC is irreversible and
  normally only used to reset a test/demo environment from scratch:
  `kubectl delete pvc data-vcf-billing-postgres-0 -n vcf-billing`.

## Troubleshooting

- **502/504 from `vcf-billing-frontend`**: `kubectl logs -n vcf-billing
  deploy/vcf-billing-api` to confirm the API actually started and is
  `Ready`. Unlike the Docker Compose deployment, there's no DNS-caching
  trick to worry about here — the frontend's `proxy_pass` target
  (`vcf-billing-api`) is a Kubernetes Service, whose ClusterIP never
  changes even when the backing pods are replaced.
- **`vcf-billing-migrate` Job fails and `api`/`collector` never come up
  healthy**: `kubectl logs -n vcf-billing job/vcf-billing-migrate`. This is
  almost always a mismatch between `DATABASE_URL` and
  `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` in `01-secret.yaml`.
- **`vcf-billing-postgres-0` never becomes `Ready`**: `kubectl describe pod
  -n vcf-billing vcf-billing-postgres-0` — most often a PVC stuck `Pending`
  because the cluster has no default StorageClass. Set
  `storageClassName` explicitly in `03-postgres.yaml`'s
  `volumeClaimTemplates`.
- **Integration sync fails with `[Errno -3] Temporary failure in name
  resolution`**: same underlying issue as the Docker Compose deployment's
  DNS troubleshooting section, but the fix is different in Kubernetes.
  Confirm it first:

  ```bash
  kubectl exec -n vcf-billing deploy/vcf-billing-api -- getent hosts <your-vcf-ops-hostname>
  ```

  If that fails, most clusters' CoreDNS already forwards unresolved names
  upstream to the node's own DNS and this isn't needed — but if your VCF
  Operations hostname lives in a DNS zone CoreDNS can't reach, uncomment and
  fill in the `dnsConfig`/`dnsPolicy` block at the top of
  `05-api.yaml`/`06-collector.yaml`'s pod spec with your internal DNS server
  IP, then re-apply and re-test the same way.
- **`ImagePullBackOff`**: either the registry needs `imagePullSecrets` (see
  step 1) or the cluster's nodes can't reach the registry at all (check with
  `crictl pull <image>` on a node, or your CNI's egress policy).

## Limitations

- No Helm chart / Kustomize overlays — every value that would normally be a
  `values.yaml` field is a literal in these YAML files, edited directly
  (`sed` or a text editor), consistent with this project's "no extra
  tooling required" deployment philosophy (see `docs/docker-deploy.md` and
  `legacy/legacy-ubuntu-deploy.md`).
- No NetworkPolicy, PodDisruptionBudget, or HorizontalPodAutoscaler — add
  these yourself if your cluster's policies require them; nothing in the
  application assumes their absence.
- `01-secret.yaml` is plain `stringData` — protected only by your cluster's
  RBAC and (if configured) etcd encryption at rest, same trust model as a
  `.env` file on a Docker Compose host. If your organization requires
  stronger secret management, replace this Secret object with one managed
  by sealed-secrets, External Secrets Operator, or similar — nothing else
  in these manifests needs to change, since they only reference the Secret
  by name.
- Not verified against a real running cluster in this environment (no
  outbound access to a Kubernetes distribution's install script or a local
  container runtime here) — every manifest was validated against the
  Kubernetes 1.30 API schema and cross-checked for consistent
  names/labels/selectors/references, and the embedded nginx config was
  syntax-checked with `nginx -t`, but an actual `kubectl apply` end-to-end
  run on your cluster is still worth doing carefully the first time (see
  the design doc's v4.11 section for exactly what was and wasn't checked).

## Where to go next

- README.md — feature reference, admin UI walkthrough, real VCF Operations
  integration guide, known limitations.
- `docs/docker-deploy.md` — the single-server Docker Compose deployment,
  if you don't have a Kubernetes cluster.
