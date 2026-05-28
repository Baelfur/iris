# Architecture

IRIS is a FastAPI app split into a shared core (`core/`) and per-variant adapters (one per database, under `variants/`). The Python package itself is `core` (import path); the outer directory is `core/`. This page covers the runtime shape, the dependency-injection pattern, and how to add a new variant.

```
core/
  core/
    routes/                        ← FastAPI routers
      inventory.py   ← /{schema}/{table}  (dynamic read layer)
      queries.py     ← /queries, /queries/{path}  (custom YAML SQL)
      readiness.py   ← /ready, /readyz  (DB-ping liveness)
      admin.py       ← /admin/refresh-schema, /admin/reload-config, /admin/pool-sizing  (X-Admin-Token gated)
    auth/                          ← three credential lanes
      user.py        ← Authorization: Bearer JWT  (user-facing)
      admin.py       ← X-Admin-Token              (operator)
      creds.py       ← X-DB-Authorization Basic   (passthrough)
    engine/                        ← SQL emission + validation
      query_engine.py    ← build_query(), paramstyle-aware
      expression.py      ← closed-grammar $filter / $having parser
      paramstyle.py      ← shared BindAccumulator
      schema_cache.py    ← DDL cache + validate_*()
      pool_sizing.py     ← startup pool-sizing report
      circuit_breaker.py ← optional async breaker around DB calls (CIRCUIT_BREAKER__ENABLED)
    observability/                 ← logging + metrics + tracing
      logging_config.py  ← structured JSON w/ database tag
      metrics.py         ← optional Prometheus /metrics  (ENABLE_METRICS, [metrics] extra)
      tracing.py         ← optional OpenTelemetry tracing (OTEL_EXPORTER_OTLP_ENDPOINT, [tracing] extra)
      kafka_logging.py   ← optional Kafka log-stream sink (KAFKA_BROKERS, [kafka] extra)
    errors/                        ← error shaping + classification + handler
      messages.py    ← error_msg, db_error_body, add_verbose_context
      exceptions.py  ← DatabaseError (variant-side wraps native DB exceptions in this)
      handler.py     ← FastAPI exception handler registration
      classify.py    ← driver-error → stable db.* code mapping (ERROR_DETAIL=safe)
    config/                        ← deployment configuration
      settings.py    ← AppSettings (shared env vars; variants extend)
      source.py      ← validation/ + queries/ loader (local / git)
      source_db.py   ← CONFIG__SOURCE=db implementation (lazy-imported, needs psycopg)
    loaders/                       ← YAML loaders (filesystem → in-memory state)
      validation.py  ← validation/<schema>/<table>.yaml → ViewDef
      queries.py     ← queries/<path>.yaml → QueryDef + get_query / list_queries
    testing/                       ← shared variant test scaffolding
      integration_suite.py ← shared integration-test bodies
      fixtures.py          ← shared pytest fixtures (client, passthrough_creds_bad)
    app_meta.py        ← build_app() factory used by every variant main.py
    context.py         ← AppContext DI
    redact.py          ← HMAC username redaction
    startup_warnings.py ← lifespan startup misconfiguration warnings

variants/{variant}/
  app/
    main.py          ← FastAPI app, lifespan wiring
    config.py        ← Settings extends AppSettings
    db.py            ← pool, fetch_all, ping, harvest_ddl
  tests/
  k8s/
  queries/           ← optional custom SQL endpoints
  validation/        ← optional per-table YAML constraints
  requirements.txt
  Dockerfile
```

---

## Why split core + variants

Every variant does the same things: build URL params into SQL, validate against DDL, parse YAML definitions, emit structured logs. The only things that differ per DB are:

- Connection pool library and lifecycle
- The DDL harvest query (`information_schema` vs `ALL_TAB_COLUMNS`)
- Paramstyle (`%(name)s` vs `:name` vs `?`)
- Pagination SQL
- Driver-specific exception types

Putting the shared logic in a core package and the differences in a thin per-variant adapter makes "add a new database" ~100 lines instead of forking the whole app. The Trino variant was the proof.

---

## Dependency injection: AppContext

`core` doesn't import any variant. Instead, each variant builds an `AppContext` at startup and registers it:

```python
# core/context.py
@dataclass
class AppContext:
    fetch_all: FetchAll
    harvest_ddl: HarvestDDL
    paramstyle: str           # "pyformat" | "named" | "qmark"
    settings: Any             # variant's Settings (must expose shared fields)
    database: str             # "postgresql" | "mysql" | "mariadb" | "oracle" | "trino"
    fetch_all_with_creds: Optional[FetchAllWithCreds] = None
    ping: Optional[Ping] = None
    get_connection_limit: Optional[GetConnectionLimit] = None  # (limit, source_label)
    breaker: Optional[CircuitBreaker] = None  # populated when CIRCUIT_BREAKER__ENABLED
```

```python
# {variant}/app/main.py (postgres example)
set_context(AppContext(
    fetch_all=db.fetch_all,
    harvest_ddl=db.harvest_ddl,
    paramstyle="pyformat",
    settings=settings,
    database="postgresql",
    fetch_all_with_creds=db.fetch_all_with_creds,
    ping=db.ping,
    get_connection_limit=db.get_connection_limit,
    breaker=circuit_breaker.from_settings(settings),
))
```

Shared routes call `get_context()` at request time. The context is a module-level singleton — simple, fast, no framework magic.

**What this buys:**

- `core` has zero per-variant branches — the paramstyle field drives SQL generation, the callables abstract DB calls
- A variant adds/omits features (passthrough, ping) by providing or not providing the corresponding callable
- Tests can inject a fake context without a real DB (see `TestReadiness` in any variant's `test_unit.py`)

---

## Paramstyles

The query engine emits SQL placeholders in whatever syntax the driver expects. `build_query()` takes `paramstyle` and branches three ways:

| Style | Placeholder | Bind shape | Used by |
|---|---|---|---|
| `pyformat` | `%(p_col)s` | `dict` | PostgreSQL, MySQL, MariaDB |
| `named` | `:p_col` | `dict` | Oracle |
| `qmark` | `?` | `list` | Trino |

The `qmark` path returns a positional list of bind values (order matches `?` order in SQL). The dict-style paths return a dict keyed by bind name. Each variant's `fetch_all` accepts whichever shape makes sense for its driver.

### Pagination

Paginations differ too:

| Style | SQL |
|---|---|
| `pyformat` (PG/MySQL/Maria) | `LIMIT {count} OFFSET {start}` (MySQL requires LIMIT first) |
| `named` (Oracle) | `OFFSET {start} ROWS FETCH NEXT {count} ROWS ONLY` |
| `qmark` (Trino) | `OFFSET {start} LIMIT {count}` (Trino requires OFFSET first) |

One engine, three emit paths. No per-variant branching elsewhere.

---

## Request lifecycle

For a request like `GET /public/products?$select=id,name&$filter=price gt 10`:

1. **FastAPI routing** — dispatched to `core.routes.inventory.query_view`. (Routes are registered in this order: `admin_router` → `queries_router` → `readiness_router` → `inventory_router`. The catch-all 2-segment inventory route is last so `/queries/<path>` and `/admin/<path>` reach their specific handlers first.)
2. **JWT verification** (when `AUTH__MODE=jwt`) — rejects 401/403 early. Applies to inventory + named-query routes only.
3. **Allowlist gate (when configured)** — the cache narrowing already happened at startup if `allowlist.yaml` was present and `ALLOWLIST__MODE=enforce` (the default). The DDL cache validation in step 5 is what enforces it at request time — a request for a schema/table outside the allowlist hits the same 404 path as a request for a schema/table that doesn't exist. `ALLOWLIST__MODE=presentation` is a documentation-only filter on the OpenAPI spec; the cache is unchanged and direct-URL access works for unlisted tables.
4. **View definition check** — if `validation/{schema}/{table}.yaml` exists, enforce required/optional params
5. **DDL cache validation** — column names in `$select`, `$orderby`, `$groupby`, safe params must exist in the cache; `$select` ⊆ `$groupby` is enforced when `$groupby` is present; `$having` requires `$groupby`
6. **Build SQL** — `query_engine.build_query(schema, view_name, params, paramstyle=ctx.paramstyle)`. Inside that: `$filter` and `$having` (if present) are parsed by `core.engine.expression` — every identifier re-validated against the DDL cache, every literal bound as a parameter (`f0`, `f1`, …) via the shared `core.engine.paramstyle.BindAccumulator`
7. **Credential passthrough check** — if `X-DB-Authorization` present and variant exposes `fetch_all_with_creds`, use it; otherwise the pool
8. **Execute** — `fetch_all` or `fetch_all_with_creds`
9. **Error handling** — driver exceptions wrapped as `DatabaseError`; full text in logs (with usernames HMAC-redacted under `LOG_USER_SECRET`); response body sanitized to `"Query failed"` under the default `ERROR_DETAIL=terse` (verbose returns the raw text)
10. **Response** — JSON with `name`, `elements`, and `links` if more pages exist

`/admin/*` routes use a different gate: `verify_admin_token(request)` checks `X-Admin-Token` against `AUTH__ADMIN_TOKEN` via `hmac.compare_digest`. JWT and DB-passthrough headers are ignored on the admin lane. `/admin/refresh-schema`, `/admin/reload-config`, `/admin/pool-sizing`, and the `/queries` catalog listing all flow through this gate.

---

## Expression parser

`core.engine.expression` is a recursive-descent parser for the closed grammar used by `$filter` and `$having`. Two responsibilities:

1. **Validation** — every identifier is passed to a caller-supplied validator (typically `schema_cache.validate_columns`) at parse time. An unknown column aborts with `ExpressionError` before any SQL is emitted.
2. **Emission** — literals become bind parameters via an `Emitter` helper. The Emitter knows the paramstyle (`pyformat` / `named` / `qmark`) and returns the right placeholder shape (`%(f0)s` / `:f0` / `?`) while accumulating the bind values into the caller's container.

The parser has no knowledge of variants or connection pools. It takes a text input, a validator callback, and an emitter, and returns a SQL fragment. `build_query` wires it up by providing the validator (bound to the current schema+table) and the emitter (bound to the request's paramstyle).

Grammar reference: [Using IRIS / The `$filter` grammar](../user-guide/using.md#the-filter-grammar). Module source: `core/core/engine/expression.py`.

---

## DDL cache

On startup:

1. Each variant's `db.harvest_ddl()` queries the metadata catalog for every column in every table the service account can `SELECT` on. `WHERE table_schema NOT IN (<per-variant SYSTEM_SCHEMAS>)` excludes the DB's built-in / system schemas; the DB's role grants drive the rest. The `allowlist.yaml`-based narrowing is applied **after** harvest by `core.loaders.allowlist.narrow_cache(...)` rather than by the harvest query — operators dropping an `allowlist.yaml` at the config root scope the cache without rewriting the harvest SQL.
2. The result is stored in a module-level dict: `schema → table → {column names}`
3. Startup logs a count: `DDL harvest complete: 47 tables cached`

At request time, `schema_cache.validate_table()` and `validate_columns()` do in-memory lookups (microseconds).

### Refreshing without restart

```
POST /admin/refresh-schema
```

Re-runs `harvest_ddl()` and replaces the cache atomically. Use after DDL changes to the target DB.

### Case handling

- Variants that handle identifiers as case-insensitive (Postgres, MySQL, MariaDB, Trino) store lowercase
- Oracle stores lowercase in the cache but normalizes UP to uppercase in emitted SQL
- User requests are case-insensitive — `/HR/EMPLOYEES` and `/hr/employees` both work

---

## Readiness probe

`/health` returns `{"status": "ok"}` the moment the FastAPI process can respond. That's what k8s uses for `livenessProbe`. When `DEPLOYMENT_NAME` is set, the response also carries `"deployment": "<name>"`.

`/ready` (alias `/readyz`) actually pings the database via `ctx.ping()`:

- Each variant's `db.ping()` runs the lightest valid query (`SELECT 1`, or `SELECT 1 FROM DUAL` for Oracle)
- Wrapped in `asyncio.wait_for(..., timeout=READINESS_TIMEOUT_MS/1000)`
- Failure or timeout → 503 `{"status": "not ready", "reason": "..."}`
- `READINESS_TIMEOUT_MS=0` → skip the DB hit, return 200 (for operators who want to disable the probe without changing k8s manifests)
- Both shapes include `"deployment"` when `DEPLOYMENT_NAME` is set

This is what k8s should use for `readinessProbe` — failing pods drain from the service endpoint without being killed, so they can recover once the DB comes back.

---

## Exception wrapping

Every variant's `fetch_all` wraps its native DB exception (`psycopg.DatabaseError`, `pymysql.DatabaseError`, `oracledb.DatabaseError`, `aiotrino.exceptions.DatabaseError`) in `core.errors.exceptions.DatabaseError`:

```python
# variants/postgres/app/db.py
try:
    ...
except psycopg.DatabaseError as exc:
    raise DatabaseError(str(exc).strip()) from exc
```

Shared route code only ever catches `DatabaseError`. This means:

- Routes stay variant-agnostic
- Adding a variant doesn't require patching existing exception handling
- The wrapped message is what ends up in the log / response (after redaction, if applicable)

---

## Logging

`core.observability.logging_config.setup_logging(database="postgresql", deployment_name=...)` installs a JSON formatter that writes one log line per event to stdout. Every line includes:

```json
{
  "timestamp": "2026-04-22T21:42:19.812Z",
  "level": "INFO",
  "logger": "iris",
  "module": "main",
  "message": "GET /public/products 200 93.73ms",
  "database": "postgresql",
  "host": "app-postgres-7d8b9-abcde",
  "deployment": "inventory"
}
```

- `database` is set per variant — aggregated logs from multiple IRIS variants can be filtered/grouped per backend without parsing the message field.
- `host` is `socket.gethostname()`, typically the pod name on Kubernetes; demuxes multi-pod log streams without producer-side tagging.
- `deployment` is the canonical IRIS deployment identity, present only when `DEPLOYMENT_NAME` is set.
- `module` is the Python module that emitted the record (`main`, `circuit_breaker`, `kafka_logging`, ...).

Request logs, errors, lifespan events, DDL harvests — all go through the same formatter.

---

## Adding a new variant

Roughly:

1. **Create the directory** — `{variant}/app/{main,db,config}.py`, `requirements.txt`, `Dockerfile`, `k8s/`
2. **`config.py`** — extend `AppSettings` with the DB-specific env vars
3. **`db.py`** — implement
   - `init_pool()` / `close_pool()` / `get_pool()`
   - `fetch_all(sql, params)` — catch native errors, raise `DatabaseError`
   - `harvest_ddl()` — return the `SchemaMap` shape
   - `ping()` — `SELECT 1` or the equivalent
   - optionally `fetch_all_with_creds(...)` for passthrough
4. **`main.py`** — copy from any existing variant; only change is `database="..."`, `paramstyle="..."`, and the imports from `. import db`
5. **`Dockerfile`** — same multi-stage template (build on `python:3.12-slim`, runtime on `${BASE_IMAGE}` with `PYTHONPATH=/opt/deps`); change the variant paths in the `COPY` lines
6. **`requirements.txt`** — add the async driver
7. **Tests** — copy an existing variant's `test_unit.py` and retarget the paramstyle (it's small — only paramstyle-specific SQL emission). Add a `tests/conftest.py` that registers `pytest_plugins = ["core.testing.fixtures"]` and provides only the variant-specific overrides (`schema_path`, `products_path`, `passthrough_creds_good`); the shared `client` and `passthrough_creds_bad` come from the plugin. Add a `tests/test_integration.py` that imports `*` from `core.testing.integration_suite`. The shared suites in `core/tests/` and `core.testing.integration_suite` already cover paramstyle-independent behavior once per run. Add a container to `test-infra/docker-compose.yml` for CI/local integration runs.
8. **Docs** — add an entry to `docs/reference/variants.md` and the variants table in `README.md`.

Things to watch for:

- **Paramstyle** — if the driver uses something not in `pyformat` / `named` / `qmark`, you'll need to add a branch to `build_query()` and the custom-query translator
- **Pagination syntax** — ditto for `build_query`
- **Type coercion** — Trino is strict about `varchar = integer`; its variant pre-coerces URL strings. Other strict-typing DBs may need similar handling.
- **Identifier case** — Oracle uppercases unquoted identifiers internally; other DBs lowercase them. If the target behaves like Oracle, follow that pattern.

---

## Release pipeline

Two workflows, two responsibilities. The split is deliberate: version computation is metadata work that needs to coordinate with merged PRs; image publication is heavy I/O that needs to fan out across variants and architectures.

### `.github/workflows/release-please.yml` — version + tag

Runs on every push to `main`. Uses `googleapis/release-please-action@v4` in manifest mode. On each run, the action takes one of three branches:

- **No standing release PR exists, releasable commits since last tag** → opens a `chore(main): release vX.Y.Z` PR carrying label `autorelease: pending`. The PR's diff bumps `.release-please-manifest.json` and `core/pyproject.toml` (via the `extra-files` toml jsonpath `$.project.version`). `skip-changelog: true` keeps `CHANGELOG.md` out of the PR.
- **Standing release PR exists, new commits since last update** → re-computes the next version from Conventional Commits prefixes (`fix:`/`feat:`/`feat!:`) and force-pushes the branch.
- **Merged PR with `autorelease: pending` label exists** (i.e., the release PR was just merged) → tags the merge commit `vX.Y.Z`, creates a GitHub Release with an auto-generated body, swaps the label to `autorelease: tagged`.

The version computation is the standard Conventional Commits → SemVer mapping; commits with non-releasable prefixes (`chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `style:`) do not open or update a release PR.

### Why a PAT, not `GITHUB_TOKEN`

Tag pushes by `GITHUB_TOKEN` do not trigger downstream workflows — GitHub's anti-loop policy. Without a PAT, `release-please` would tag `vX.Y.Z` but `release.yml` would never fire. The workflow reads `RELEASE_PLEASE_TOKEN` (PAT with `repo` + `workflow` scope) when present and falls back to `GITHUB_TOKEN` for environments where someone is willing to manually `workflow_dispatch` the build.

### Pre-tag gates (the "tag is the last step" rule)

Before a tag is ever created, three layers of validation run on the PRs that lead to it:

1. **`.github/workflows/validate-build.yml`** — fires on every PR targeting `main` (i.e., `dev → main` promotion PRs). Builds all 5 variants single-arch with `push: false`. Required check via branch protection — the dev → main merge cannot complete unless every variant builds cleanly. Catches Dockerfile syntax, base-image resolution, pip-install failures, COPY-path regressions on the source state about to land on `main`.
2. **`.github/workflows/variant-integration.yml`** — fires on every PR targeting `main`. Each variant gets its own job with a live DB service container; the variant's `test_integration.py` runs against the seeded DB. Catches variant-level regressions that unit tests can't (passthrough auth, custom queries, real driver behavior). Oracle marked `continue-on-error` initially because GHA + Oracle XE is known-flaky on first-boot timing.
3. **`.github/workflows/build-smoke.yml`** — fires on PRs from `release-please--*` branches (and on Dockerfile-affecting changes generally). Single variant, fast. Required check on release PRs — catches the rare case where something between dev → main merge and release PR merge breaks the build (upstream tag flip, manifest bump pulling a broken transitive dep, etc.).

Both gates use `push: false` — they don't publish anything to GHCR. The full multi-arch publish happens *after* the tag is created, in `release.yml`. By that point the source state is already validated; tag-time failures are limited to transport (registry auth, network, qemu emulation flakes) and recoverable by re-running the failed matrix job.

The "tag is the last step" rule: build validation must pass on the PR before the merge that creates the tag. If validation fails, the PR doesn't merge, the version doesn't increment, the tag doesn't get created. No "version-bump-without-images" stuck state.

### `.github/workflows/release.yml` — build + publish

Triggers on tag pushes matching `v*` (and on pushes to `main` for the `:main` rolling tag, and `workflow_dispatch` for backfills). On a tag push:

1. **Build matrix** fans out across the 5 variants × 2 architectures (`linux/amd64`, `linux/arm64`) using QEMU + Buildx. GHA-cached pip layers keep multi-arch tractable. Pushes to `ghcr.io/baelfur/iris-{variant}:{X.Y.Z}` and, for stable releases (no `-` in the version), `:latest`.
2. **Release job** (gated on `needs: build`) checks out `main` (with PAT so it can push later), extracts the `## [Unreleased]` section from `CHANGELOG.md` via awk as the Release body (falls back to `## [X.Y.Z]` for backfills / re-runs / manual rename cases, then a placeholder if neither has content), and upserts the GitHub Release: if `gh release view` succeeds (release-please created it on tag push), `gh release edit --notes-file` overwrites the auto-generated commit-list body with the curated CHANGELOG section. If not (e.g., manual tag push without release-please), `gh release create` makes one from scratch.
3. **CHANGELOG close-out** (final step, gated on Release publication succeeding) renames `## [Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`, prepends a fresh empty `## [Unreleased]` for the next cycle, and pushes the bot commit `chore: close [Unreleased] -> [X.Y.Z] in CHANGELOG` to `main`. Skipped when the `[X.Y.Z]` section already exists (operator pre-renamed, or this is a re-run).

### Why the CHANGELOG close-out lives in `release.yml`

The body extraction needs hand-curated content; release-please's auto-generated commit-list body would be plainer. previously, the operator had to rename `[Unreleased]` → `[X.Y.Z]` manually on the release-please branch before merging — a fragile step that lived between two clicks and was missed twice (v2.1.3, v3.0.0). Moving the rename into `release.yml` itself closes the gap: the workflow that ships the release also closes the CHANGELOG section. Operators never have to remember the rename, and re-runs / manual backfills are idempotent (the rename is skipped when `[X.Y.Z]` already exists).

The bot commit on `main` is `chore:` — release-please's commit-search ignores it for version computation, so the close-out doesn't trigger another release cycle.

### Failure recovery

- **`release.yml` didn't fire on a tag push** (e.g., the tag predated PAT configuration): use the `workflow_dispatch` trigger or delete + re-push the tag locally.
- **GHCR build failed mid-matrix**: re-run the failed matrix job; image tags are per-variant so a partial publish is recoverable.
- **Release body is the placeholder**: edit the CHANGELOG to add the `[X.Y.Z]` section, then `gh release edit vX.Y.Z --notes-file` with the extracted block.
- **CHANGELOG close-out push failed** (race with concurrent `main` push, or PAT permissions): the Release was already published — the body is correct. Just push the rename manually: rename `[Unreleased]` → `[X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md` and commit. The next release run will skip the rename since `[X.Y.Z]` exists.

See [Versioning](versioning.md) for the maintainer flow and the SemVer contract.

## Repository layout — vendor metadata

Vendor-specific repo metadata lives in per-vendor namespaces rather than at the repo root:

- `.github/` — GitHub-side metadata (workflows under `.github/workflows/`, Dependabot config).
- `.gitlab/` — GitLab-side metadata (issue templates under `.gitlab/issue_templates/`, MR templates under `.gitlab/merge_request_templates/`). The CI config (`.gitlab-ci.yml`) stays at the repo root because GitLab requires it there; everything else GitLab-flavored lives under `.gitlab/`.

Convention exists so future contributors don't put GitLab metadata at the repo root or GitHub metadata in `docs/`. When a new vendor surface is wired (CODEOWNERS, GitLab K8s Agent, a Renovate config), it goes in the matching namespace.

---

## Related

- [Variants](variants.md) — the quirks each current variant had to accommodate
- [Versioning](versioning.md) — semver contract and what counts as the public API
- [Security posture](../user-guide/security-posture.md) — the threat model the architecture is built around
- [Operating IRIS](../user-guide/operating.md) — how variants actually run in prod
