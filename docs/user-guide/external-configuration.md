# External configuration

How IRIS reads `validation/` and `queries/` YAMLs at runtime, and how that shapes your deployment story. If you're picking up IRIS for the first time, read this before you decide how to ship it.

## Two adoption shapes

Most IRIS deployments fall into one of two patterns. Picking the right one upfront saves rework.

### Shape A — consume the upstream image, manage config externally

Your team pulls `ghcr.io/baelfur/iris-{variant}:X.Y.Z` directly. You don't fork the IRIS repo. Your config lives in a place you own — a config repo, a config Postgres — and IRIS reads from there at startup.

What you own:
- k8s manifests (probably as Helm or Kustomize)
- A separate config repo or config database
- Image registry pull-config

What you don't own:
- IRIS itself
- The image build

Upgrade path: bump the image tag in your manifest, rolling deploy. That's it. Your config repo is unchanged.

### Shape B — fork the upstream repo

You clone `Baelfur/iris` and edit in place — the in-repo `validation/`, `queries/`, k8s manifests, possibly the Dockerfile. You build your own images and push them to your registry.

What you own:
- The fork (with diff against upstream)
- Your image build pipeline
- Everything in the repo

Upgrade path: rebase your fork against the new upstream tag, resolve conflicts in any file you've edited, rebuild images, deploy.

### Hybrid

Many real deployments are a blend. Fork minimally for deployment-platform stuff (k8s manifests, base images, internal-registry policy), but pull config from a separate config repo via `CONFIG__SOURCE=git`. That cuts the fork's diff against upstream by ~80% and keeps the high-frequency-change content (validations, queries) out of your rebase path.

For most internal-data-platform deployments — especially "many developers, few SREs" — hybrid is the right call.

## Picking a source

`CONFIG__SOURCE` selects where IRIS reads YAMLs from. Three values:

| Source | When to pick |
|---|---|
| `local` | Quickstart, evaluation, dev environments where image-baked YAMLs are fine. Single-team static deployments where the YAMLs change rarely and operators are happy editing them in the repo. |
| `git` | The recommended production pattern for most teams. Devs PR YAML changes to a config repo; merge; `POST /admin/reload-config` on the IRIS instance. No pod restart, no image rebuild. |
| `db` | When SQL is the management surface, when one config Postgres should serve many IRIS deployments with database-level isolation, or when audit logging via Postgres triggers is the workflow you want. |

## Pattern: local

IRIS reads `validation/` and `queries/` directories from disk at startup. This is what you get with `CONFIG__SOURCE=local`. The env var is required — operators set it explicitly so the source is never an accident.

By default IRIS looks at the working directory (`.`). Operators bake their own YAMLs into the image at build time (downstream Dockerfile `COPY` step), or bind-mount a host directory at the working directory in a `docker run`, or — most flexibly — set `CONFIG__LOCAL_ROOT` to point IRIS at a different path entirely.

```bash
# YAMLs baked into the image at /opt/myapp/config (downstream Dockerfile COPY)
docker run -d \
  -e PG_HOST=... -e PG_USER=... -e PG_PASSWORD=... -e PG_DATABASE=... \
  -e CONFIG__SOURCE=local \
  -e CONFIG__LOCAL_ROOT=/opt/myapp/config \
  ghcr.io/baelfur/app-postgres:X.Y.Z
```

```bash
# YAMLs bind-mounted from host
docker run -d \
  -e PG_HOST=... -e PG_USER=... -e PG_PASSWORD=... -e PG_DATABASE=... \
  -e CONFIG__SOURCE=local \
  -v /host/path/to/iris-config:/app/iris-config:ro \
  -e CONFIG__LOCAL_ROOT=/app/iris-config \
  ghcr.io/baelfur/app-postgres:X.Y.Z
```

Add a YAML → re-mount or rebuild → call `POST /admin/reload-config` (or restart). That's the loop.

The published variant images do **not** ship demo YAMLs — the variant directory in the upstream repo is pure production code; demo content lives under `tests/fixtures/` and isn't copied into the image. Operators supply their own YAMLs deliberately rather than receiving sample content by accident.

For evaluation and small deployments, this is fine. The friction shows up when you have devs who want to add a custom query without touching the IRIS image build pipeline. That's the moment to switch to `git` or `db`.

## Pattern: git

`CONFIG__SOURCE=git` makes IRIS clone an external git repo on startup and read YAMLs from there. The image stays unchanged across config edits.

### Configuration

```bash
CONFIG__SOURCE=git
CONFIG_GIT_URL=https://github.com/your-org/iris-inventory-config.git
CONFIG_GIT_BRANCH=main           # default; override per environment
CONFIG_GIT_TOKEN=<PAT>           # only for HTTPS-private repos
```

### Repo layout

The config repo's tree mirrors the in-image layout:

```
your-iris-config-repo/
  validation/
    public/
      products.yaml
      orders.yaml
  queries/
    reports/
      products_by_category.yaml
      revenue_by_region.yaml
```

Same shape operators are already familiar with from the in-repo path. Copy your existing `validation/` and `queries/` into a new repo and you're done.

### Auth

| Scheme | How |
|---|---|
| HTTPS public repo | Leave `CONFIG__GIT_TOKEN` unset |
| HTTPS private repo | Set `CONFIG__GIT_TOKEN` to a PAT. IRIS injects `oauth2:<token>` into the URL. Works with GitHub, GitLab, Bitbucket. |
| SSH | Use an `ssh://` URL and mount a deploy key at the standard `~/.ssh/` path in the pod. The token env var is HTTPS-only. |

### Lifecycle

1. **Lifespan startup** — IRIS shallow-clones the configured branch into a tmpdir via `dulwich` (pure-Python, no system `git` binary needed in the image).
2. **Loaders read** from the cloned tree.
3. **`POST /admin/reload-config`** (admin-token-gated) pulls the configured branch and re-runs the loaders. Devs PR a YAML, merge, then:

```bash
curl -X POST -H "X-Admin-Token: $AUTH__ADMIN_TOKEN" \
  https://iris.example.com/admin/reload-config
```

No pod restart. No `kubectl`.

### Trust boundary

The config repo becomes the lever for changing validation rules and custom-query SQL. Whoever can merge to the configured branch can change behavior. Treat the repo like the IRIS code itself:

- Code-review YAML PRs
- Restrict who can merge
- Run any SQL-shape linters you have in CI on the repo

Pulling YAMLs externally trades "fork the IRIS image to change a YAML" friction for "manage write access to a separate repo" responsibility. Pick the side that matches your org's workflow.

## Pattern: db

`CONFIG__SOURCE=db` makes IRIS read YAMLs from a Postgres config server. Per-deployment isolation: each IRIS instance has its own database on the config server, named after `DEPLOYMENT_NAME`.

### Why database-per-deployment

One config Postgres can serve N IRIS deployments without co-mingling rows or sharing RBAC. `GRANT CONNECT ON DATABASE inventory TO iris_inventory_user`. Per-deployment backup via `pg_dump <deployment>`. Different config repos don't get to confuse each other.

### Configuration

```bash
CONFIG__SOURCE=db
CONFIG_DB_DSN=postgresql://iris_config:secret@config-pg:5432/postgres
DEPLOYMENT_NAME=inventory
```

`DEPLOYMENT_NAME` is required here — it's the per-deployment database name. Validated against Kubernetes-style identifier rules (`^[a-z][a-z0-9_-]{0,62}$`; hyphens normalized to underscores only at the config-DB Postgres-identifier site). The same name also feeds log records, OTel `service.name`, and the `X-{App}-Deployment` response header — one canonical identity across the deployment. The `App` segment derives from `APP_NAME` (default `"app"`; set to whatever brand the surrounding product uses).

### Provisioning the config Postgres

A one-time setup on the config Postgres server, by whoever has admin on it:

```sql
-- Create a service account for IRIS with CREATEDB
CREATE ROLE iris_config WITH LOGIN PASSWORD 'choose-a-real-secret' CREATEDB;
```

That's it. IRIS does the rest on first startup.

The `CREATEDB` grant is the security-relevant decision — it lets IRIS create new databases on this Postgres server. That's deliberate; without it, IRIS can't bootstrap a new deployment on first run. If your security posture forbids `CREATEDB`, pre-create each deployment database yourself and grant `iris_config` `CONNECT` + table-creation privs on each.

### Schema (created on first startup if missing)

```sql
-- in database <deployment_name>
CREATE TABLE iris_config_validations (
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    required JSONB NOT NULL DEFAULT '[]',
    optional JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (schema_name, table_name)
);

CREATE TABLE iris_config_queries (
    path TEXT NOT NULL,
    sql TEXT NOT NULL,
    required JSONB NOT NULL DEFAULT '[]',
    optional JSONB NOT NULL DEFAULT '[]',
    allow_writes BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (path)
);
```

### Bootstrap flow

On first startup, IRIS:

1. Connects to the admin database (`postgres`) at `CONFIG__DB_DSN` with autocommit (Postgres rejects `CREATE DATABASE` inside a transaction).
2. Checks if `<deployment_name>` exists in `pg_database`.
3. `CREATE DATABASE "<deployment_name>"` if missing.
4. Reconnects to `<deployment_name>`.
5. `CREATE TABLE IF NOT EXISTS` for both tables.
6. Queries existing rows; loaders consume.

Logs at INFO during the steps so operators can see what happened the first time.

### Adding config

Insert rows in the relevant deployment's database. Standard SQL:

```sql
-- in database `inventory`

INSERT INTO iris_config_validations (schema_name, table_name, required, optional)
VALUES ('public', 'products',
        '["id"]'::jsonb,
        '["status"]'::jsonb);

INSERT INTO iris_config_queries (path, sql, required, optional, allow_writes)
VALUES ('reports/by_id',
        'SELECT id, name, price FROM public.products WHERE id = :id',
        '["id"]'::jsonb,
        '[]'::jsonb,
        FALSE);
```

Then `POST /admin/reload-config` on the IRIS instance to pick them up. No pod restart.

### Multi-deployment example

One config Postgres server, two IRIS instances, fully isolated:

```
config-pg:5432
├── database: inventory       ← app-postgres-inventory reads from here
│   ├── iris_config_validations
│   └── iris_config_queries
└── database: billing         ← app-postgres-billing reads from here
    ├── iris_config_validations
    └── iris_config_queries
```

Each instance sets `DEPLOYMENT_NAME=inventory` or `DEPLOYMENT_NAME=billing` respectively. They never see each other's rows. RBAC at the database level — `iris_inventory_user` has no grants on `billing` and vice versa.

### Trust boundary

The config DB joins the YAML repo (for git source) as a place where config changes happen. Whoever has `INSERT/UPDATE/DELETE` on the two tables can change validation rules and custom-query SQL.

Database-level RBAC is the right lever. Grant `INSERT` on `iris_config_queries` only to the trusted committers. Use Postgres audit logging or `pgaudit` to record changes for compliance trails. Same posture as the git option, different mechanism.

## Migration recipes

### local → git

The most common upgrade path, especially after a deployment outgrows the image-baked workflow.

1. **Mirror your current YAMLs to a new git repo.** Copy `your-fork/validation/` and `your-fork/queries/` into a fresh repo, preserving the directory tree. Push.
2. **Set the env vars** on a single dev pod first:
   ```
   CONFIG__SOURCE=git
   CONFIG_GIT_URL=https://github.com/your-org/iris-config.git
   CONFIG_GIT_BRANCH=main
   CONFIG_GIT_TOKEN=<PAT>
   ```
3. **Rolling deploy** the dev pod. Watch the lifespan log for "Cloning config repo ... Loaded N view definition(s)" — same counts as before.
4. **Smoke test** the dev pod against known queries. The behavior should be identical because the YAMLs are identical.
5. **Roll out to UAT and prod** when you're satisfied. Same env vars, same config repo, different image tags.

After the rollout you can delete `validation/` and `queries/` from your IRIS fork — they're no longer the source of truth.

### local → db

Similar shape, more upfront SQL setup.

1. **Provision the config Postgres** (see above). Pre-create the deployment database if your `CREATEDB` posture doesn't allow IRIS to do it.
2. **Translate your YAMLs into SQL inserts.** Each `validation/<schema>/<table>.yaml` becomes a row in `iris_config_validations`; each `queries/<path>.yaml` becomes a row in `iris_config_queries`. A short script can do this if you have a lot of files.
3. **Set env vars** on a dev pod:
   ```
   CONFIG__SOURCE=db
   CONFIG_DB_DSN=postgresql://iris_config:secret@config-pg:5432/postgres
   DEPLOYMENT_NAME=inventory
   ```
4. **Rolling deploy.** Lifespan log shows "Config DB 'inventory' materialized: N validation row(s), M query/queries."
5. **Smoke test** and roll forward.

## Upgrade path: dev → UAT → prod

The promotion mechanics are standard image-tag flow, with one wrinkle if you're on `git` or `db`.

### For Shape A (consume image + external config)

Each environment has its own k8s manifest with its own image tag:

```
dev/deployment.yaml      → image: app-postgres:1.1.0-rc.1
uat/deployment.yaml      → image: app-postgres:1.1.0
prod/deployment.yaml     → image: app-postgres:1.1.0
```

For `git` source, the config repo can be branched per environment too:

```
config-repo:
  config/dev   ← experimental, frequently changing
  config/uat   ← cherry-picked from dev after dev validation
  config/main  ← prod (or whatever your main-branch convention is)
```

Then per-environment env: `CONFIG_GIT_BRANCH=config/dev` for dev pods, etc. Or one branch with environment-keyed YAMLs (less flexible but simpler).

For `db` source, separate deployments naturally separate config: `DEPLOYMENT_NAME=iris_dev` vs `iris_uat` vs `iris_prod` each have their own database on the config Postgres.

### For Shape B (fork)

Add to the standard fork-rebase flow:

1. `git fetch upstream v1.1.0`
2. `git checkout -b merge/v1.1.0 main`
3. `git merge upstream/v1.1.0`
4. **Resolve conflicts.** Likely places:
   - `validation/` if upstream changed test fixtures
   - `queries/` same
   - `k8s/` if upstream changed defaults
   - `Dockerfile` if upstream changed the install line (this happened in 1.0.x)
5. `docker build` your variants, tag, push to your registry
6. dev → UAT → prod via image-tag rollout, same as Shape A

The fork's diff against upstream is the work surface. Hybrid (fork minimally + `CONFIG__SOURCE=git` for content) cuts this dramatically.

## Forward compatibility

A `1.x` IRIS image is expected to keep working against config written for the same minor release. The contract is enforced two ways: hand-curated `CHANGELOG.md` discipline at the editorial layer, and an automated promotion harness at the test layer that brings up the prior image, seeds config + DDL representative of the previous release, then promotes the image in place and re-runs the same surface to check that nothing regresses across the upgrade. The harness runs per variant in CI.

What's safe across a minor upgrade:
- New optional env vars with documented defaults that preserve old behavior
- New `/admin/*` endpoints
- New response fields (consumers must ignore unknown fields)

What requires a major bump:
- Changing existing env-var defaults in a way that changes behavior
- Removing endpoints or response fields
- Tightening the URL grammar

See `docs/reference/versioning.md` for the full contract.

## Trust-boundary summary

| Source | What controls config changes |
|---|---|
| `local` | Whoever can edit the IRIS repo and push a new image |
| `git` | Whoever can merge to the configured branch of the config repo |
| `db` | Whoever has `INSERT/UPDATE/DELETE` on the two config tables |

In every case, the writer of config is a trust boundary alongside the writer of YAMLs in the IRIS repo itself. Apply your usual discipline: code review (or SQL review), restricted committers, audit logging.

`security-posture.md` covers the broader threat model around custom-query YAMLs; the trust boundary moves but the rules don't.

## Troubleshooting

### `CONFIG__SOURCE=git`: "fatal: could not read Username for ..."

The repo is HTTPS-private and `CONFIG__GIT_TOKEN` isn't set, or is set to an expired/wrong PAT. Either set a valid token or switch to a public repo / SSH URL.

### `CONFIG__SOURCE=git`: "Couldn't find remote ref refs/heads/<branch>"

`CONFIG__GIT_BRANCH` doesn't exist in the repo. Default is `main`; older repos may use `master`. Set the env var to match the repo's actual default branch.

### `CONFIG__SOURCE=db`: "permission denied for database postgres"

The IRIS service account can connect but can't create the deployment database. Either grant `CREATEDB`:

```sql
ALTER ROLE iris_config CREATEDB;
```

…or pre-create the deployment database yourself and grant the service account `CONNECT` on it.

### `CONFIG__SOURCE=db`: bootstrap is slow on first start

First-startup is `CREATE DATABASE` + `CREATE TABLE` × 2 + `SELECT * FROM` × 2 — usually under a second on local Postgres, longer if the config server is far away. Subsequent restarts skip the create step and just query, so the first cold-start latency is one-shot.

### Loaders pick up zero rows after `/admin/reload-config`

The configured branch / database has no rows. Verify by listing with the source's native tooling (`git log --stat` on the config repo, `SELECT * FROM iris_config_validations` on the config DB). If there are rows but IRIS doesn't see them, check the IRIS lifespan logs for the bootstrap path it actually hit.

### `CONFIG__SOURCE=db` won't start: "DEPLOYMENT_NAME for db source must be a valid Postgres identifier"

The name doesn't match `^[a-z][a-z0-9_]{0,62}$`. Lowercase only, alphanumeric + underscore, ≤63 chars, starts with a letter. `inventory_prod` is valid; `Inventory-Prod` is not. The constraint matches Postgres identifier rules so the name can be a database name without quoting.
