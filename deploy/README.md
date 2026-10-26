# Deploying spanreed

Kustomize, two layers. `base` is portable. `overlays/kholinar` holds the facts
about one cluster: which node holds the volume, what hostname the UI answers
on, and which image tag is deployed.

ArgoCD points at the overlay.

```
deploy/
├── base/                     portable
└── overlays/kholinar/        this cluster
```

Render it before you believe it:

```sh
kubectl kustomize deploy/overlays/kholinar
```

There is no `kubectl apply` in any workflow here. ArgoCD applies what is in
git, and anything applied by hand is drift that ArgoCD will undo.

---

## What you have to provide

### 1. The Secret

**Nothing secret is committed to this repo, and no example Secret manifest
belongs here either.** The workloads reference a Secret by name and expect it
to already exist in the namespace, created by whatever mechanism this cluster
already uses: sealed-secrets, external-secrets, SOPS, or a one time
`kubectl create secret` done outside the GitOps flow.

Name: `spanreed-secrets`, in namespace `spanreed`.

| Key | Required | What it is |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Without it the composer still runs, but every brief is degraded |
| `BASIC_AUTH_PASSWORD` | yes | The single UI credential. Unset means the UI is unauthenticated |
| `SMTP_HOST` | no | Email delivery is skipped cleanly when unset |
| `SMTP_PORT` | no | Defaults to 587 |
| `SMTP_USER` | no | |
| `SMTP_PASSWORD` | no | |
| `SMTP_FROM` | no | Required for email, along with host and to |
| `SMTP_TO` | no | |

Keys are injected with `envFrom.secretRef`, so a key absent from the Secret is
simply an environment variable that is not set, and the matching setting falls
back to its default. That is what makes the optional rows optional.

The user name is not a secret and lives in the ConfigMap as `BASIC_AUTH_USER`.

### 2. The node label

All three workloads mount the same `ReadWriteOnce` volume, so all three have to
land on the same node. The overlay pins them with
`kubernetes.io/hostname: kholinar`.

If you would rather pin by role than by hostname, `base` selects
`spanreed.stmcallister.dev/data-node: "true"` instead. Label the node and drop
the two nodeSelector patches from the overlay:

```sh
kubectl label node kholinar spanreed.stmcallister.dev/data-node=true
```

### 3. The image

```sh
docker build -t ghcr.io/stmcallister/spanreed:v0.1.0 .
docker push ghcr.io/stmcallister/spanreed:v0.1.0
```

Then bump `newTag` in `overlays/kholinar/kustomization.yaml`. That tag is the
deployed version, and `latest` in `base` is only a placeholder for anyone
rendering `base` on its own.

### 4. Things to edit in the overlay

- the ingress host, currently `spanreed.kholinar.ts.net`
- the ingress class, currently `traefik`, which is what k3s ships
- the image tag

---

## The ArgoCD Application

This does not live in this repo. It belongs wherever the cluster's other
Application manifests live.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: spanreed
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/stmcallister/spanreed.git
    targetRevision: main
    path: deploy/overlays/kholinar
  destination:
    server: https://kubernetes.default.svc
    namespace: spanreed
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

---

## What runs

| Workload | Kind | Schedule | Notes |
|---|---|---|---|
| `spanreed-collector` | CronJob | every 3 hours | Exits 0 even when sources fail |
| `spanreed-composer` | CronJob | 06:00 America/Los_Angeles | Reads only, never fetches |
| `spanreed-web` | Deployment | always on | Readiness on `/healthz` |

One image, three entrypoints. The command is always `python -m spanreed` and
the argument picks the workload.

`activeDeadlineSeconds` matters more than it looks on both CronJobs. Without
it, a hung feed leaves a pod running until the next schedule, and with
`concurrencyPolicy: Forbid` that silently skips runs.

The web Deployment uses `strategy: Recreate`, not the default RollingUpdate. A
second pod cannot mount the ReadWriteOnce volume while the first still holds
it, so a rolling update would deadlock on every deploy.

Migrations run at container start in all three entrypoints, inside one
immediate transaction guarded by the `schema_version` table. All three starting
at once is safe.

---

## Delivery

Three ways out, in this order:

1. **Archive**, always. The brief is written to the database before anything
   else is attempted, so a delivery that fails costs a delivery and never a
   brief.
2. **Email**, only when SMTP is configured. Unconfigured is a normal state and
   the composer says so once at INFO. Port 465 is treated as implicit TLS,
   anything else tries STARTTLS and carries on unencrypted with a warning if
   the server does not offer it. `SMTP_TO` may name several addresses,
   separated by commas.
3. **RSS** at `/feed.xml`, carrying the last 30 briefs.

The feed sits behind the same basic auth as the rest of the UI, because it
carries the whole brief rather than a link to it. Readers that accept a
credential in the URL can subscribe:

```
https://spanreed:PASSWORD@spanreed.kholinar.ts.net/feed.xml
```

---

## Recovering the database

The PVC is `local-path`, which means it is a directory on one node and it is
not backed up. The briefs are small and the items are disposable, but the
topics and sources are configuration you typed by hand.

`seed/topics.yaml` bootstraps an empty database on first run. It is read once,
on a database with no topics at all, and ignored from then on. The image
carries it at `/app/seed/topics.yaml`, which is what `SEED_PATH` points at.

To capture the live configuration, go the other way:

```sh
kubectl exec -n spanreed deploy/spanreed-web -- \
  python -m spanreed export-topics > seed/topics.yaml
```

That writes YAML to stdout, with its logs on stderr so the redirect stays
clean, and the file it produces is one this same bootstrap can read back. Cache
bookkeeping such as ETags is left out, because it is worthless to a database
being restored. Commit the result and a lost volume costs a rebuild rather than
retyping every relevance hint.

The command is read only. It does not bootstrap, so exporting from an empty
database gives you an empty file rather than the seed reflected back.
