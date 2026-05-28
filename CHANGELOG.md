# Changelog

All notable changes to IRIS are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). See [Versioning](docs/reference/versioning.md) for what counts as a breaking change.

## [Unreleased]

## [1.0.1] - 2026-05-28

### Added

- **Apache-2.0 LICENSE.** Repository is now licensed under Apache-2.0 — explicit patent grant, permissive use/modification/redistribution, attribution required. `core/pyproject.toml` carries the SPDX license expression. Downstream consumers (forks, internal redistributions) can build on the codebase under the standard Apache 2.0 terms.

- **OCI image labels (`source`, `url`, `revision`, `licenses`) baked into release builds.** Every published image now carries `org.opencontainers.image.source` pointing at the current repo, plus `url`, `revision` (git SHA), and `licenses=Apache-2.0`. GHCR's package "Repository source" field reads from this label, so the package pages now correctly link back to `Baelfur/iris` instead of inheriting the sticky source from the pre-fork publish history.

### Changed

- **Image name moved from `ghcr.io/baelfur/app-{variant}` → `ghcr.io/baelfur/iris-public-{variant}`.** v1.0.0 briefly published to `app-{variant}` during the public-launch transition; from v1.0.1 forward, the canonical pull URL is `ghcr.io/baelfur/iris-public-{variant}:X.Y.Z`. The `app-` prefix was a brand-scrub artifact from pre-public dev (v8.0.0–v8.2.0 on iris-archive). The `iris-public-` prefix clearly identifies these as the publicly-released line and avoids inherited package state from the archive. Operators who pulled v1.0.0 from `app-{variant}` should update their image references; the `app-{variant}:1.0.0` tag remains pullable but won't receive further updates.

## [1.0.0] - 2026-05-28

### Added

- **Initial public release.**

  Read-only HTTP façade over relational databases via a closed-grammar URL DSL. Every table in an allowed schema becomes a paginated, filterable JSON endpoint without writing endpoint code. Five database variants ship: PostgreSQL, MySQL, MariaDB, Oracle, Trino.

  **Query surface.** `GET /{schema}/{table}` accepts `$filter` / `$having` (closed expression grammar — comparisons, `in` lists, nulls, boolean combinators, nothing more), `$select` / `$orderby` / `$groupby` (DDL-cache-validated column lists), `$count` + `$start_index` (offset pagination), `$cursor` (HMAC-signed keyset pagination for large walks). Simple `?col=value` filters compose with the reserved params. Custom SQL queries live in operator-authored YAML and surface at `/queries/<path>`.

  **Auth.** Three credential lanes that compose: `AUTH__MODE=jwt|gateway|open` for user-facing posture; `X-DB-Authorization: Basic` for per-request DB credential passthrough; `X-Admin-Token` or hybrid SSO (JWT-with-admin-group) for `/admin/*`. Settings are required (no implicit defaults), so deployments declare an explicit posture rather than inheriting one by omission.

  **Configuration sources.** Three modes via `CONFIG__SOURCE`: `local` (filesystem), `git` (pulls from an external repo), or `db` (per-deployment Postgres). Hot-reload via `POST /admin/reload-config`.

  **Observability.** Structured JSON logs to stdout (level via `LOG_LEVEL`); optional Kafka log streaming (`KAFKA__BROKERS`); optional OpenTelemetry tracing (`OTEL_EXPORTER_OTLP_ENDPOINT`) with HTTP-level spans and opt-in DB-level instrumentation (`ENABLE_DB_TRACING`); optional Prometheus `/metrics` (`ENABLE_METRICS`).

  **Brand-agnostic.** Setting `APP_NAME` cascades to the HTTP response header (`X-{Brand}-Deployment`), OTel `service.name`, Kafka `client.id`, JSON log `app` field, Prometheus metric prefix, and config-DB table names. Forks rebrand by changing one env var.

  **OpenAPI.** Per-deployment dynamic spec at `/openapi.json` lists the actual harvested schemas and tables. Three visibility modes (`OPENAPI__VISIBILITY=enabled|admin-enabled|disabled`) cover trusted networks, closed-network deployments, and operators who ship generated SDKs to consumers separately.

---

## Pre-1.0 history

The sections below reflect iteration in the project's pre-public phase. They preserve the original CHANGELOG content with section headers renumbered into the `0.x.y` range to signal "pre-1.0 development"; each header carries the original archive tag in italics for traceability. Builds for the archive tags remain pullable at `ghcr.io/baelfur/app-{variant}:vN.M.K` per the archive repo's release history.

## [0.8.2] - 2026-05-28 _(archive: v8.2.0)_

### Added

- **`LOG_LEVEL` env var** (#356). Root-logger level is now operator-configurable — pre-fix it was hardcoded to `INFO` in `setup_logging`. Accepts the standard Python levels (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) case-insensitively (`LOG_LEVEL=debug` works). Default `INFO` preserves pre-#356 behavior. The `uvicorn.access` logger is independently pinned at `WARNING` regardless — it duplicates the service's per-request log line and stays silenced. Documented in `operating.md`'s env-var table.

## [0.8.1] - 2026-05-28 _(archive: v8.1.0)_

### Added

- **Per-variant DB-level OpenTelemetry spans** (`ENABLE_DB_TRACING`) (#250). Closes the request-span hierarchy the original tracing PR explicitly deferred. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set AND `ENABLE_DB_TRACING=true`, supported variants register their driver-level OpenTelemetry instrumentor so DB calls emit child spans nested under the FastAPI request span. Buys DB-vs-middleware time breakdown and pool-checkout-wait visibility in the trace UI. Default off because (a) the closed-grammar architecture makes the SQL derivable from URL + path + catalog without driver-level spans (incremental visibility, not baseline), and (b) DB spans include `db.statement` with the executed SQL by default — sensitive when passthrough usernames and WHERE values land in the observability backend. Per-variant coverage: **postgres** (psycopg, supported), **oracle** (oracledb, best-effort — async-mode failures swallowed at WARNING), **mysql/mariadb** (no PyPI-published OTel instrumentor for `aiomysql`; documented gap with INFO log), **trino** (no official `aiotrino` instrumentor; documented gap with INFO log). HTTP request spans continue to work for gap-case variants — URL + path encodes the query for trace-side derivation. Documented in `operating.md`.

- **Hybrid SSO admin lane** (#303). The `/admin/*` gate now accepts either `X-Admin-Token` (the existing shared static secret) **or** a Bearer JWT carrying a configured admin group claim. New settings: `AUTH__ADMIN_GROUP` (claim value granting admin), `AUTH__ADMIN_CLAIM_NAME` (which claim to look in — default `groups`; `roles`, `scope`, etc. supported with `scope` handled as space-delimited per OAuth convention), and three OIDC URLs (`AUTH__OIDC_AUTH_URL`/`TOKEN_URL`/`CLIENT_ID`) that, when all set, declare an `oauth2` securityScheme in the admin OpenAPI spec so Swagger UI's Authorize button runs the OIDC popup against the IDP. Both paths coexist — token wins when both headers are present (gateway-injected token over user-supplied JWT). Settings unset = today's behavior bit-for-bit; opt-in by configuring `AUTH__ADMIN_GROUP`. JWT-admin path validates against `AUTH__JWKS_URL`; settings validator rejects `AUTH__ADMIN_GROUP` without `AUTH__JWKS_URL` at startup. Buys per-user audit (resolved identity logged at INFO on accept) and eliminates the shared-secret-in-Slack pattern for deployments without a gateway. Documented in `operating.md`.

- **Cursor-based pagination** (`$cursor`) for large result-set walks (#244). Offset pagination (`$start_index`) is O(n²) for full walks because the database re-evaluates the query and discards leading rows on every page. The new keyset-style `$cursor` is O(log n) per page given a real index — designed for agent / ETL / LLM workloads walking thousands to millions of rows. Responses with `$orderby` set and a full page now include a `cursor` field; pass it back as `$cursor` on the next request. Cursors are HMAC-signed against an operator-configured `CURSOR_SECRET` (a per-process random key is generated when unset — operators running >1 replica or needing cursors to survive restarts set it explicitly). Mutual exclusion: `$cursor` + `$start_index` together is 400. Orderby binding: the cursor encodes the issuing `$orderby` and is rejected on mismatch. NULL-ordering caveats and unique-tiebreaker recommendations in `using.md`. Purely additive — `$start_index` continues to work unchanged.

## [0.8.0] - 2026-05-28 _(archive: v8.0.0)_

### Changed (BREAKING)

- **App-name-agnostic refactor: `APP_NAME` setting cascades to external surfaces** (#262, #313). New root setting controls the application's brand identity so the surrounding product (not the project) decides how operators see it. Cascades to:
  - HTTP response header — `X-Iris-Deployment` → `X-{Slug}-Deployment` where `Slug` derives from `APP_NAME`. Default `APP_NAME="app"` yields `X-App-Deployment`. Operators on the historical brand restore with `APP_NAME=iris` (header becomes `X-Iris-Deployment` again).
  - FastAPI app title / OpenAPI spec title — `"IRIS"` → title-cased `APP_NAME`.
  - OTel `service.name` fallback — `iris-<deployment>` → `<app>-<deployment>`.
  - Kafka `client.id` default — `iris-<deployment>-<host>` → `<app>-<deployment>-<host>`.
  - Prometheus metric prefix — `iris_*` → `{app_name}_*` (#321).
  - Config-DB table names — `iris_config_validations` / `iris_config_queries` → `{app_name}_config_validations` / `{app_name}_config_queries` (#322). Migration-recovery path reads the old names when they exist so in-place upgrades work.

  Module loggers switched from `getLogger("iris")` to `getLogger(__name__)`, so logger names are now per-module. The JSON log envelope gains an `app` field (driven from `APP_NAME`). Runtime-facing strings neutralized; migration-recovery references to historical names preserved as in-code historical context. The "IRIS" brand is no longer baked into anything an operator or API client sees by default. Migration: operators who depend on the legacy header name / metric prefix / table names set `APP_NAME=iris`. Pre-1.0 / pre-public.

- **Python module + distribution renamed `iris_core`/`iris-core` → `core`/`core`** (#313, #318). `from iris_core.x import y` → `from core.x import y`; `pip install ./core` produces a `core` distribution. Class renames: `IrisBaseSettings` → `AppSettings` (#319), `IrisContext` → `AppContext`, `IrisDatabaseError` → `DatabaseError` (#317).

- **CI / fixtures / image tags rebranded** (#313). docker-compose service `iris-config-pg` → `config-pg`, seed users `iris_meta`/`iris_pass` → `metadata_user`/`passthrough_user`, env vars `IRIS_TEST_KAFKA_BROKERS`/`IRIS_TEST_CONFIG_DSN` → `TEST_KAFKA_BROKERS`/`TEST_CONFIG_DSN`, GitLab CI job `iris-core-tests` → `core-tests`, published image `ghcr.io/baelfur/iris-{variant}` → `ghcr.io/baelfur/app-{variant}`, entrypoint script `iris-entrypoint.py` → `entrypoint.py` with `IRIS_DRY_RUN` → `DRY_RUN`.

### Added

- **Jinja2 doc templating with brand cascading + CI drift check** (#316). Every operator-facing doc has a `.md.tmpl` source rendered with `docs/_config.yml` defaults (`app_name`, `display_name`, `header_slug`). Both source and rendered `.md` are committed; `docs/build.py --check` (wired into `.github/workflows/lint.yml`) blocks drift. A fork can change `display_name` and rebuild to rebrand the docs without code changes.

## [0.7.0] - 2026-05-27 _(archive: v7.0.0)_

### Security

- **`pip-audit` ignore for `PYSEC-2026-161` (starlette BadHost CVE)**. Real CVE-2026-48710 — missing Host header validation in starlette < 1.0.1 can let an attacker poison `request.url.path` and bypass path-derived auth checks. Upstream blocker: `prometheus-fastapi-instrumentator` (latest 7.1.0) pins `starlette<1.0.0`, so the `[metrics]` extra prevents bumping. IRIS auth reads explicit headers only (`Authorization` / `X-DB-Authorization` / `X-Admin-Token`) — no Host-header-derived URL reconstruction anywhere — so the exploit path does not reach the threat surface. Ignore is documented in-place in `.github/workflows/unit-tests.yml`; remove when `prometheus-fastapi-instrumentator` releases a starlette-1.0-compatible version. Tracked in #307.

### Changed (BREAKING)

- **Closed-grammar query params consolidated into one declaration** (#272). The seven `$select` / `$filter` / `$orderby` / `$count` / `$start_index` / `$groupby` / `$having` params are now declared once in `iris_core/engine/query_params.py` as the `ClosedGrammarParams` FastAPI dependency. The canonical route (`routes/inventory.py`), alias routes (`aliases.py`), the Trino 3-segment route (`variants/trino/app/main.py`), and the per-deployment OpenAPI spec (`openapi_dynamic.py`) all derive from it. Adding a new closed-grammar param now requires editing one file instead of four; a guard test (`test_query_params.py`) pins the names and constraints so silent drift is impossible. Marked breaking because handler signatures changed shape (`ClosedGrammarParams` dependency instead of seven individual `Query(...)` params) — relevant only for code that imports the route handlers directly.

- **Unified error envelope** (#245). All error responses now use the same JSON shape: `{"error": {"code": "<class>", "message": "<text>", ...extras}}`. Validation/auth codes are derived from HTTP status (`validation.bad_request`, `validation.not_found`, `auth.unauthorized`, `auth.forbidden`, `validation.unprocessable`, `server.error`); driver codes are the existing `db.<class>` set from `iris_core.errors.classify`. Removes the prior split between `{"detail": "..."}` (validation) and `{"error": ...}` (driver) — clients now branch on `error.code` instead of which key is present. Verbose-mode `deployment` and `database` debug fields remain as siblings of `error`. `using-iris.md` and `security-posture.md` updated to describe the unified shape. Pre-1.0 / pre-public means the breaking change is free.

- **Default-deny posture across auth-relevant settings** (#261). Three asymmetric defaults flipped fail-closed:
  - **`AUTH__MODE` is now a required setting** (no default). Operators declare a posture explicitly: `jwt` (IRIS validates JWT against `AUTH__JWKS_URL`), `gateway` (accepts any caller, assumes upstream validation), or `open` (accepts any caller, no upstream assumption — dev only; logs WARNING). Replaces the implicit "JWKS unset → no auth" posture, which silently accepted unauthenticated callers when an operator forgot to read the recommendation. **Migration**: existing deployments with `AUTH__JWKS_URL` unset must add `AUTH__MODE=gateway` (or `open` if appropriate); deployments with JWKS configured set `AUTH__MODE=jwt`. Settings validation rejects `mode=jwt` without `jwks_url`.
  - **`AUTH__REQUIRE_PASSTHROUGH` defaults `true`.** Data routes (`/{schema}/{table}` and `/queries/<path>`) now 401 without `X-DB-Authorization` (or `Authorization: Basic`) before reaching the DB. Fail-closed against deployments running with a service account that has full data SELECT — without this flag, every unauthenticated caller silently inherited the service account's privileges. Operators with metadata-only service accounts who want pool-mode access for some calls explicitly set `AUTH__REQUIRE_PASSTHROUGH=false`.
  - **`MAX_PAGE_SIZE` defaults `10000`** (was `0`/unbounded). Operators who genuinely want unbounded set `MAX_PAGE_SIZE=0` explicitly. The previous unbounded default protected nobody from a misbehaving client requesting a million rows.

  All three are pre-1.0 / pre-public so the breaking flavor is acceptable. Each fail-closed default emits a startup WARNING when explicitly disabled (`AUTH__MODE=open`, `AUTH__REQUIRE_PASSTHROUGH=false`, `MAX_PAGE_SIZE=0`) so operators see the posture they've chosen. Variant `.env.example` files updated with the new fields and migration notes; `operating-iris.md` Authentication section rewritten to document the three modes.

### Added

- **`did_you_mean` hints on 404s and column-validation 400s** (#255). When a request references a typo'd schema, table, custom-query path, or column that closely matches a real one in the DDL cache (`difflib.get_close_matches` with cutoff 0.6), the error response surfaces a `did_you_mean` field alongside the message. Schema/table/query 404s get a single suggestion; column-list 400s (`$select` / `$orderby` / `$groupby` / simple-filter) get a parallel-order list (one suggestion per bad column, omitted entirely when no column is close enough). Conservative threshold — `produts` → `products` matches, `xyzzy` doesn't.

- **`DEPLOYMENT_NAME` accepts hyphens** (#254). Kubernetes / Helm / Docker names use hyphens pervasively; the validator now accepts `^[a-z][a-z0-9_-]{0,62}$`. The hyphenated value passes through to `X-Iris-Deployment` header, log records, `/health`, `/ready`, and OTel `service.name` unchanged. When `CONFIG__SOURCE=db`, `DbSource` normalizes hyphens to underscores for the per-deployment Postgres database name (unquoted identifier rules), logging the substitution at INFO. The raw form stays on `DbSource.deployment_name`; the normalized form is `DbSource.db_name`.

- **Cold-checkout bootstrap shortcut** (#242). New `Makefile` exposes `make setup` (editable install with `[test]` extras), `make test`, and `make lint` targets so a fresh contributor or automated reviewer can land in a runnable state with one command. README's "Testing" section points at it. The longhand `pip install -e './core[test]'` flow remains for fine-grained use.

- `docs/user-guide/operating-iris.md` gains a "Putting SSO in front of IRIS" section: recommended pattern (`oauth2-proxy` / `pomerium` / cloud IAP / ingress-controller plugin in front of IRIS), example sidecar config, ingress-controller alternative, verification checks, and a note on why a built-in OIDC flow isn't pre-built. `security-posture.md`'s "Authentication at the HTTP edge" out-of-scope item links to the new section. (closes #259)

- `docs/user-guide/operating-iris.md` gains a "Gating the admin lane through a gateway" section. The recommended pattern: gateway handles authentication / authorization however the org does it (SSO, mTLS, internal API keys, JIT IAM, signed requests — whatever fits) on its public side, then injects the static `X-Admin-Token` header on requests that passed the gateway's checks. IRIS sees a normal token-authed request. Calls out the anti-pattern (sharing one token across the team), names the gateway features that support static-header injection (Kong `request-transformer`, API Umbrella headers, Tyk middleware, nginx `proxy_set_header`, etc.), and covers two ways to recover audit identity (correlate by request ID at the gateway log, or forward an `X-Forwarded-User` header).

- `docs/user-guide/operating-iris.md` gains a "Restricting who can reach IRIS" section. Explains why IRIS doesn't have an application-layer IP allowlist (source IP at the HTTP layer is the proxy's, not the caller's, once any proxy is in front) and maps the right network-fabric layer per deployment shape: Kubernetes `NetworkPolicy` (label-based, with example), cloud-vendor security groups / firewall rules (tag-based), on-prem VM host firewalls, single-host docker compose (drop the `ports:` declaration). Calls out the Docker iptables gotcha — Docker's `DOCKER-USER` chain executes before ufw / firewalld rules, so `-p 8000:8000` can bypass the host firewall. Two fixes documented: insert rules in `DOCKER-USER`, or bind to a specific interface (`-p 127.0.0.1:8000:8000`).

- **Defense-in-depth on the admin sub-app** (#260). `app_meta.build_app` now constructs the admin FastAPI sub-app with `dependencies=[Depends(verify_admin_token)]`, so every route mounted under `/admin/*` is gated by the token check at the dependency layer — independent of whether each handler also calls `verify_admin_token()` itself. A future endpoint that forgets the per-handler call can no longer silently expose itself; the per-handler calls remain as belt-and-braces.

- **JWT validation hygiene tests** (#260). New `core/tests/test_auth_user.py` pins three properties of `verify_token`:
  - `alg: none` tokens are rejected (no signature verification can succeed under `algorithms=["RS256"]`).
  - HS256 tokens are rejected — closes the alg-confusion attack class regardless of what HMAC secret the attacker chose.
  - `mode="gateway"` and `mode="open"` short-circuit without reading the Authorization header, while `mode="jwt"` rejects requests with no Bearer header (and rejects `Authorization: Basic` since it isn't Bearer). The behavior was correct pre-test; the test locks it in so a future refactor can't silently widen the algorithm list or skip the missing-credentials path.

- **Catalog enumeration utility** (`core/iris_core/catalog.py`). Single source of truth for walking the harvested catalog. Pre-#256 `/admin/catalog` and `openapi_dynamic` each iterated the same DDL cache + view-def + custom-query state with their own loop; when the catalog gains a new dimension (row-level policies, write-mode flags, tags, etc.) both sites had to update in lockstep. Both consumers now project `iter_tables()` / `iter_queries()` results into their respective output shapes; future consumers of the catalog get the same enumeration for free. (closes #256)

- **Configurable OpenAPI spec visibility** (`OPENAPI__VISIBILITY`) (#302). New literal setting with three modes:
  - `enabled` (default — today's behavior): `/docs`, `/redoc`, `/openapi.json` all reachable unauthenticated.
  - `admin-enabled`: HTML pages return 404; `/openapi.json` requires `X-Admin-Token`. Programmatic spec consumers (SDK builds, doc-generation pipelines) authenticate with the same secret that gates `/admin/*`. Browsers can't render the spec.
  - `disabled`: all three return 404. Maximum closed.

  Closes the schema-recon information-disclosure surface for non-gateway deployments. Gateway-fronted deployments already gate the URL externally; this setting is for operators without a gateway. The admin sub-app (`/admin/docs`, `/admin/openapi.json`, `/admin/redoc`) is independent of this setting — always token-gated.

- **Admin sub-app spec endpoints now actually require the admin token.** Pre-#302, `/admin/docs`, `/admin/openapi.json`, and `/admin/redoc` were publicly accessible despite the sub-app's `dependencies=[Depends(verify_admin_token)]` — FastAPI's auto-mounted spec/docs endpoints use `add_route` (not `add_api_route`), bypassing router-level dependencies. The sub-app now constructs with `openapi_url=None / docs_url=None / redoc_url=None` and re-registers all three as decorator-added routes that correctly inherit the token dependency. Pre-1.0 / pre-public, but worth flagging — the bug-fix tightens an admin-surface info-disclosure that's been present since #260 was filed.

- **Fail-loud on env-var typos in nested settings** (`extra="forbid"`) (#249). Root `IrisBaseSettings` and every submodel (`AuthSettings`, `PoolSettings`, `CircuitBreakerSettings`, `ConfigSettings`, `KafkaSettings`) now reject unknown fields at instantiation. Typos in nested env-var names (`AUTH__JWKS_LUR`, `POOL__MIN_SIE`, `KAFKA__TOPCI`) raise a `ValidationError` at lifespan startup with a clear message naming the offending field — replaces the previous silent-drop behavior where the typo was ignored and the deployment ran with default values for the field the operator intended to set. **Limitation:** pydantic-settings's `EnvSettingsSource` doesn't emit unknown flat-root env vars (`MAX_PAGE_SIE`, `DEPLOYMENT_NAEM`) as extra dict keys, so flat-root typos stay silently dropped — covered cases are the nested-submodel surface, which is where every security-relevant setting lives.

- **Allowlist mode toggle** (`ALLOWLIST__MODE`) (#291). New literal setting with two values:
  - `enforce` (default — today's behavior). `narrow_cache()` drops non-listed schemas/tables from the DDL cache; identifier validation returns 404 on any request that references them. Allowlist is a security boundary.
  - `presentation`. `narrow_cache()` is a no-op for the cache; the OpenAPI renderer applies the allowlist matchers at render time only. Full DDL surface stays reachable; the spec only documents the listed subset. Use case: 1000+ table deployments where the operator wants a curated docs surface (50 named tables in the spec) without losing direct-URL access for power users.

  `presentation` mode is **not a security boundary** — startup logs a WARNING when set, and `security-posture.md`'s control 2 calls out the distinction. Operators relying on the allowlist to gate access must use `enforce`.

## [0.6.1] - 2026-05-07 _(archive: v6.0.1)_

## [0.6.0] - 2026-05-07 _(archive: v6.0.0)_

### Changed

- **CI hygiene cluster.** Four operational additions to the CI pipeline:
  - `concurrency:` blocks added to all six workflows. Superseded runs on the same ref cancel automatically (`unit-tests`, `validate-build`, `build-smoke`, `variant-integration`); release flows (`release`, `release-please`) keep `cancel-in-progress: false` so tag pushes and main pushes don't fight. Closes the redundant-CI-noise problem flagged after the v5.0.0 release. (closes #251)
  - `pip-audit --strict` step in `unit-tests.yml` runs against the full optional-extra surface (`[test,metrics,tracing,config-git,config-db,kafka]`). Fails on any unfixed CVE. (closes #247)
  - `.github/dependabot.yml` watches `pip` (core/ + 5 variants) and `github-actions` (root) ecosystems on a weekly cadence. **Security-only**: `open-pull-requests-limit: 0` suppresses routine version-drift PRs; GitHub still raises Dependabot PRs for advisories that affect a pinned dependency. Floor pins in `pyproject.toml` and the variant `requirements.txt` files match the API surface IRIS uses, so chasing major-version bumps is busywork; `pip-audit --strict` (same workflow) is the independent CVE check. (closes #247)
  - `.github/workflows/dependabot-auto-merge.yml` enables `--auto` merge on Dependabot PRs once required checks pass. Gated by the repo variable `DEPENDABOT_AUTO_MERGE` — workflow runs on every Dependabot PR but skips the merge step unless the variable is `"true"`. Default off; flip in Settings → Secrets and variables → Actions → Variables.
  - `docs/user-guide/security-posture.md` gains a "Supply chain" section summarizing the pip-audit + Dependabot security-only + optional auto-merge posture, plus a checklist item on branch protection for `dev`.
  - Coverage gate. `pytest-cov` added to the `[test]` extra; `[tool.coverage]` configured in `core/pyproject.toml` with a `fail_under = 80` threshold (current: ~87%). `unit-tests.yml` runs with `--cov` and fails below threshold. (closes #248)
  - `lint.yml` workflow runs ruff + mypy as a required check. Baseline cleared in the same PR: 327 ruff fixes via `--fix`, 36 files reformatted, 12 mypy errors resolved (type narrowing + `# type: ignore` for known FastAPI typing gaps in dynamic-spec assignment + exception-handler signature). New code that fails lint blocks the merge. Codebase is now PEP 604 / 585-compliant (`X | None`, `list[X]`, etc.). (closes #246)

### Added

- **Spec render modes for OpenAPI scale** (`OPENAPI_RENDER_MODE`). New setting with three values addressing the 1000+ table deployment case where the per-deployment OpenAPI spec was too verbose for Swagger UI / ReDoc to render comfortably:
  - `simple-schema` (new default): collapses per-column simple-filter params into a single generic `<any-listed-column>` entry per table with the column list in the description. ~74% spec-size reduction at typical column counts.
  - `optimized-schema`: surfaces concrete simple-filter params for PK/FK/indexed columns only; non-indexed columns surface via the same generic entry as simple-schema. Operationally aligned — testers and dashboards naturally navigate by indexed columns; the spec acts as a soft hint. Random-column filtering still works at runtime via `$filter`.
  - `full-schema`: pre-#268 behavior preserved as the legacy / dev escape — every column gets its own concrete simple-filter param.
  Per-variant DDL harvest enriched with index metadata (postgres via `pg_index`, mysql/mariadb via `information_schema.STATISTICS`, oracle via `ALL_IND_COLUMNS`); Trino best-effort (`information_schema` doesn't uniformly expose index info; degrades to simple-schema layout). (closes #268)

- **YAML allowlist** for narrowing the harvested DDL surface by schema and/or table glob patterns. New `allowlist.yaml` at the config root (loaded via the same `CONFIG__SOURCE` mechanism as `validation/` and `queries/`):

  ```yaml
  schemas: [public, audit]
  tables:
    - public.fact_*
    - public.dim_customer
  ```

  Both sections optional; both support glob patterns; combine with AND (a table must satisfy both `schemas` and `tables` if both are set). `/admin/reload-config` re-applies the allowlist. **Replaces the `ALLOWED_SCHEMAS` env var.** Operators with `ALLOWED_SCHEMAS` set must move the schema list into `allowlist.yaml`'s `schemas:` section — pre-1.0 / pre-public means the breaking change is free. (closes #269)

- **Route aliases** for legacy-gateway migration. Validation YAMLs and custom-query YAMLs gain an `aliases:` list — each entry registers as an additional FastAPI route delegating to the canonical handler. The motivating use case is migrating off a legacy URL surface (data-virtualization layers, internal proxies, vendor APIs being replaced) without forcing a coordinated cutover: re-point the gateway at IRIS and consumers keep hitting their existing URLs. Reserved paths (`/health`, `/ready`, `/admin/*`, `/queries/*`, etc.) cannot be aliased over; alias-on-alias collisions are rejected at load time with clear ERROR logs; aliases that shadow `/<schema>/<table>` are accepted with a WARNING. Aliases appear in the canonical operation's OpenAPI description (option B — keeps spec lean at scale). v1 limitation: alias changes require a pod restart; `/admin/reload-config` updates contracts but not route registrations. (closes #270)

### Removed

- **`ALLOWED_SCHEMAS` env var.** Migrated to `allowlist.yaml`'s `schemas:` section (#269). Operators with the env var set silently lose narrowing on upgrade — startup logs an INFO line listing harvested schemas/tables when no `allowlist.yaml` is supplied, surfacing the unexpected dynamic surface size if the migration was missed.

### Fixed

- OpenAPI spec no longer repeats the full column list in every parameter description. Pre-fix, `$select`, `$filter`, `$orderby`, `$groupby`, and the generic simple-filter entry each enumerated every column — on a 60-column table that's the same list rendered five times in a row, producing a wall of text in Swagger UI. The column list now lives once, in the operation description; per-param descriptions reference it. View-def required-column callouts are kept inline because the constraint is per-entry semantic.

- Variant directory layout no longer leaks demo YAMLs into deployments. The per-variant `queries/` and `validation/` directories that previously sat at the variant root (next to `app/`, `Dockerfile`, etc.) have moved to `variants/<v>/tests/fixtures/`. The shipped Dockerfiles never copied them in, so published images were already clean — but any deployment pipeline that treated the variant directory as a deploy artifact (bind mounts, downstream `COPY` directives, Helm chart-from-directory templating) silently included the demo content. Now the variant root is pure production code; demo YAMLs live solely under `tests/`. Confirmed against a real operator Oracle deployment that was seeing demo `reports: category_summary` / `reports: products_by_category` queries alongside the operator's actual custom queries. (closes #267)

### Added

- `CONFIG__LOCAL_ROOT` settings field. When `CONFIG__SOURCE=local`, this controls the path IRIS reads `validation/` and `queries/` from. Defaults to `"."` (cwd, today's behavior). Operators with custom directory layouts point it elsewhere; variant integration tests use it to point at `tests/fixtures/` so test YAMLs don't have to live at the deploy artifact root. (#267)

## [0.5.1] - 2026-05-06 _(archive: v5.1.0)_

## [0.5.0] - 2026-05-06 _(archive: v5.0.0)_

## [0.4.2] - 2026-05-05 _(archive: v4.2.0)_

### Changed

- View-def required-param check now considers `$filter` equality constraints, not just simple filters. A request like `GET /public/products?$filter=id eq 1` now satisfies a view-def that requires `id` — pre-fix, the route only counted simple `?id=1` filters and rejected anything where the constraint lived inside `$filter`. The new check uses **strict equality semantics**: only `eq` and `in` count as constraining; `ne`/`gt`/`lt`/`ge`/`le`, `null` predicates, `not`, and ORs where one branch lacks the column do NOT — those are ranges/exclusions that don't bound the result set in a way the YAML's "required" intent honors. AND unions branches; OR intersects them. The check lives on a new `ViewDef.satisfied_by(simple_keys, filter_columns)` method; `ViewDef.validate` now only checks unknown params (its required-param responsibility moved to `satisfied_by`). Affects both the dynamic route (`/<schema>/<table>`) and custom queries (`/queries/<path>`). `docs/user-guide/using-iris.md` gains a "Required parameters" section explaining what filter shapes count. (closes #187)
- `iris_core/iris_core/expression.py` refactored from emit-as-it-parses to **parse → AST → emit**. The recursive-descent parser now returns AST node dataclasses (`Eq`, `Ne`, `Gt`, `Ge`, `Lt`, `Le`, `IsNull`, `IsNotNull`, `In`, `And`, `Or`, `Not`, `Paren`); `emit_sql(node, emitter)` walks the AST to produce SQL. Public `parse(text, validator, emitter)` API unchanged — same SQL output, all 34 prior parser tests still pass. The split lets `constrained_columns(node)` walk the same AST without a second parser, prep for #53's hygiene refactor.

### Removed

- `test-infra/e2e_test.sh` — superseded by `.github/workflows/variant-integration.yml` which exercises the same surface (live DBs, real HTTP, all 5 variants) automatically on every dev → main PR. Operator manual smoke is now `cd <variant> && pytest tests/test_integration.py` against a `docker compose up` stack, which is what the workflow does. `docs/user-guide/operating-iris.md:411` updated. (closes #188)

## [0.4.1] - 2026-05-05 _(archive: v4.1.0)_

### Fixed

- Postgres pool `_configure_connection` callback now `await conn.commit()` after the `SET statement_timeout` so psycopg-pool doesn't discard the connection with `connection left in status INTRANS by configure function`. The default psycopg cursor opens an implicit transaction; psycopg-pool 3.3+ requires the configure callback to leave the connection in a clean state. Pre-fix, lifespan startup would hang trying to populate the pool — every connection got configured, left INTRANS, discarded, retried, until the lifespan timed out. (#189)
- Variant integration tests: the shipped `validation/public/products.yaml` requires `id` as a simple filter, but most tests in the shared suite use only `$filter` and don't supply simple `id=1`. The result was an internally contradictory test suite — Class A tests (e.g. `test_basic_filter`) want view_defs unloaded, Class B tests (`test_yaml_*`) want them loaded. New autouse fixture `empty_view_defs_by_default` clears `view_defs._defs` after lifespan startup so most tests run unrestricted; new `view_def_required_id` fixture explicitly populates the products view def for the three Class B tests that need it. The shipped variant images still include the YAMLs unchanged. (closes #189)

### Added

- Per-base release tags. `release.yml` now publishes three image variants per tag/main-push: the default slim (`:X.Y.Z`, `:latest`, `:main`), distroless (`:X.Y.Z-distroless`, `:distroless`, `:main-distroless`), and Docker Hardened Image (`:X.Y.Z-dhi`, `:dhi`, `:main-dhi`). Slim and distroless are free + anonymous; DHI is gated on `vars.DH_AUTH_AVAILABLE == 'true'` (operator sets this after configuring `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` secrets) and skipped silently otherwise. Operators pick the tag suffix that matches their security posture instead of building hardened images themselves. (closes #175)
- Multi-base smoke matrix in `build-smoke.yml`. The Dockerfile-change gate now exercises slim, distroless, and DHI bases (DHI gated on the same auth variable). Catches base-specific regressions in the Dockerfile (e.g., a COPY path that works on slim but breaks on distroless's filesystem) before they hit a release. (#175)
- Variant integration tests gate dev → main promotion PRs. New `variant-integration.yml` runs each variant's `test_integration.py` against a live DB service container (Postgres 16, MySQL 8, MariaDB 11, Trino latest, Oracle Free slim). Each job is independent — failure in one variant doesn't take out the others. Oracle is marked `continue-on-error: true` initially because GHA + Oracle XE is known-flaky on first-boot timing; flip to required once we've seen it run green reliably. Closes the previous gap where variant-level regressions (passthrough auth, custom queries, driver-specific behaviors) only got caught manually. (closes #179)
- Build-gate the release pipeline so the tag is the last step. New `validate-build.yml` runs the full 5-variant single-arch build matrix on every PR targeting `main` (typically `dev → main` promotions) with `push: false`. Required check via branch protection — the merge button stays disabled until every variant builds cleanly. Existing `build-smoke.yml` extended to also fire on release-please PRs (paths now include `iris_core/pyproject.toml` and `.release-please-manifest.json`), giving a single-variant smoke gate at the last step before tag creation. After this lands, a build failure blocks the merge that would have created the tag — no more "version-bump-without-images" stuck state. The on-tag `release.yml` build still runs (as the actual publish step) but its job is transport, not validation. (closes #180)

## [0.4.0] - 2026-05-05 _(archive: v4.0.0)_

### Changed

- Multi-stage Dockerfile across all 5 variants. Build stage stays on `python:3.12-slim` (always — has pip, apt, build tools); runtime stage uses `ARG BASE_IMAGE=python:3.12-slim` (default unchanged) but operators can override at build time with `--build-arg BASE_IMAGE=gcr.io/distroless/python3-debian12` (free hardened) or `--build-arg BASE_IMAGE=dhi/python:3.12` (Docker Hardened Image, requires Docker Hub auth + DHI entitlement). Site-packages copied from the build stage into `/opt/iris-deps`; `PYTHONPATH` points there. Default behavior is unchanged for existing operators — same `python:3.12-slim` runtime, just now multi-stage. (closes #169)
- `iris-entrypoint.sh` (POSIX shell) replaced with `iris-entrypoint.py` (Python). Same behavior — TLS flag handling, `IRIS_DRY_RUN=1` test hook, exec uvicorn. Python instead of shell so the entrypoint runs on shell-less hardened bases (distroless). Python is present in every base image IRIS supports. (#169)

### Documentation

- Pre-1.0 doc sweep closing the audit-driven hygiene backlog. Adds `/admin/reload-config` to endpoint lists in `docs/user-guide/operating-iris.md` and `docs/reference/architecture.md`. Refreshes log envelope examples to include `host`, `module`, and conditional `deployment` fields (post-Kafka logging and `DEPLOYMENT_NAME` work). Refreshes `/health` and `/ready` payload descriptions to mention the optional `deployment` field. Documents the validation-error vs driver-error response-body shape split (`{"detail": "..."}` vs `{"error": "..."}`) in `using-iris.md` and `security-posture.md` so client code knows which key to branch on. Removes stale "promotion harness on the roadmap" reference in `external-configuration.md`. Strips load-bearing `(#NN)` issue cruft from user-facing prose in `operating-iris.md`, `external-configuration.md`, `security-posture.md`, and the README's Hello-IRIS example. Reframes the JWKS failure-mode "future enhancement" note as an as-yet-unneeded extension point. The README's Hello IRIS example uses `iris-postgres:local` as the build tag instead of pinning a specific version. Reference docs (`architecture.md`, `versioning.md`, `variants.md`) keep their `(#NN)` references — those are maintainer-facing and the citations are load-bearing context. (closes #114, #130)

### Fixed

- `iris_core/iris_core/base_settings.py` comment on `CONFIG_SOURCE` no longer claims fail-closed posture analogous to `ALLOWED_SCHEMAS`. The two are different shapes: empty `CONFIG_SOURCE` raises (fail-closed, matching `ADMIN_TOKEN`), empty `ALLOWED_SCHEMAS` is grants-driven (open default). Comment rewritten to be honest about the asymmetry. (#130 P2)

### Changed

- **BREAKING**: dropped Python 3.11 from the supported runtime matrix. `iris_core/pyproject.toml` now requires Python 3.12+, all 5 variant Dockerfiles base on `python:3.12-slim`, and CI matrices in both GitHub Actions and GitLab CI run only against 3.12. Pre-fix, 3.11 and 3.12 were both tested but the project never used a 3.11-only feature — 3.11 was carried for compatibility that no consumer actually needed. Single-version matrices halve CI cost and remove a "works on 3.11 but not 3.12" failure mode that didn't earn its keep. **Migration**: deployments on 3.11 must move to 3.12. The 3.12 release line is current upstream and will outlast 3.11 (3.11 EOL October 2027). (closes #149)
- Dependencies now carry version floors (`>=`) instead of being unpinned. Floors match the API surface IRIS actually uses; ceiling pins (`<3`) only on packages with known major-breaking-change history (`pydantic-settings`). Touches `iris_core/pyproject.toml` and all 5 variants' `requirements.txt`. Catches accidental downgrades to pre-API releases without locking us out of normal patch/minor upgrades. Lockfile + reproducible-build pinning is a follow-up that comes with the hardened-image rebase. (#45)

### Fixed

- `iris_core.config_source.GitSource.materialize` now closes the `dulwich` `Repo` returned by `porcelain.clone`. Pre-fix, the underlying `DiskObjectStore` would hold pack-file handles open until the `Repo` was garbage-collected, surfacing as `ResourceWarning: ObjectStore was destroyed with N unclosed pack(s)` at process shutdown. (#45)

## [0.3.0] - 2026-05-05 _(archive: v3.0.0)_

A major release driven by one breaking config change (`ENABLE_TRACING` removal — tracing now activates on `OTEL_EXPORTER_OTLP_ENDPOINT` presence) plus the Trino 3-segment URL path that closes the legacy-`flaskdsl-trino` migration gap, the `ALLOWED_SCHEMAS` startup info log, an `error_classify` precision tightening, and clarifying docs on `ERROR_DETAIL=safe` and `ALLOWED_SCHEMAS=[]` semantics.

**Breaking changes** (one):
- `ENABLE_TRACING` env var is no longer recognized. Operators must set `OTEL_EXPORTER_OTLP_ENDPOINT` to activate tracing. See "Changed" below for migration guidance.

### Added

- Trino: optional 3-segment URL path `/{catalog}/{schema}/{table}` alongside the existing `/{schema}/{table}`. The 3-seg form emits a fully-qualified `catalog.schema.table` SQL identifier; the catalog must match the connection's configured `TRINO_CATALOG` (case-insensitive), since the DDL cache is keyed on the configured catalog only — other catalogs return 404. Cross-catalog querying would require harvesting `information_schema` across all reachable catalogs and is out of scope. The 3-seg form matches the legacy `flaskdsl-trino` URL shape so existing consumers can drop in IRIS without rewriting client URLs. Other variants are unaffected: Postgres connections are scoped to one database, MySQL/MariaDB don't have a third level (`database == schema`), and Oracle is `schema/table`. (closes #152)
- Startup INFO log when `ALLOWED_SCHEMAS` is unset or empty, showing the harvested schema/table counts that the grants-driven default produced. Addresses the v0.x → "remove the env var because it's optional now" footgun: an operator who previously had `ALLOWED_SCHEMAS=["public"]` and removes it on upgrade silently exposes whatever else the service account has `SELECT` on. The new log line surfaces the resulting surface size at startup so the change is visible without having to query the DDL endpoint. (#135)

### Changed

- **BREAKING**: tracing now activates on `OTEL_EXPORTER_OTLP_ENDPOINT` presence, not the removed `ENABLE_TRACING=true` toggle. Set the collector endpoint to enable tracing; unset means off. The activation rule across IRIS sinks: sinks with a required destination config presence-detect on it (Kafka on `KAFKA_BROKERS`, tracing on `OTEL_EXPORTER_OTLP_ENDPOINT`); sinks without one use `ENABLE_*` toggles (metrics, because Prometheus is pull-style and has no destination URL). Removes the double-positive footgun where an operator could set `OTEL_EXPORTER_OTLP_ENDPOINT` and forget to also flip `ENABLE_TRACING`. **Migration**: drop `ENABLE_TRACING=true` from your env (it's now a no-op); set `OTEL_EXPORTER_OTLP_ENDPOINT=<collector-url>` if you weren't already. If you had `ENABLE_TRACING=true` without an explicit endpoint, you were silently exporting to `http://localhost:4317` and the change makes that explicit. (closes #134)
- Documented `ALLOWED_SCHEMAS=[]` semantics: empty is treated identically to unset (DB grants drive scope). There is no env-var way to express "expose nothing" — `REVOKE` at the DB is the canonical lever, consistent with IRIS's threat model where DB grants are the load-bearing security boundary. `docs/user-guide/operating-iris.md` and `docs/user-guide/security-posture.md` updated. (#135)
- Clarified `ERROR_DETAIL=safe` scope and code-list policy in `docs/user-guide/operating-iris.md` and `docs/user-guide/security-posture.md`. The mode shapes only **wrapped DB-driver errors**; validation errors (invalid column, unknown schema, missing required parameter, expression-grammar errors) continue to echo full detail in every mode by design — they're caller-input feedback, not server-state leaks, and opaqueness there would make every 4xx undebuggable from the client side. The `db.*` codes (`db.bad_credentials`, `db.connection_refused`, `db.permission_denied`, `db.timeout`, `db.query_failed`) are an **open list**: clients should treat unknown codes as `db.query_failed`-equivalent for fallback. Renaming or removing a code is breaking; adding one is not. No behavior change. (#133)

### Fixed

- `error_classify` now uses word-boundary regex instead of simple substring matching when mapping driver text to `db.*` codes. Pre-fix, an identifier like a column or table name that contained a pattern phrase as a substring (e.g., `my_max_statement_time_value`) could trigger the pattern's classification. Word boundaries (`\b`) close that gap for snake_case identifier collisions; residual risk remains where driver text echoes a pattern phrase verbatim (e.g., user data quoted in the error message), and clients should continue to treat the `db.*` codes as best-effort per the open-list contract documented in security-posture.md control 10. (closes #136)

## [0.2.5] - 2026-05-05 _(archive: v2.1.3)_

### Fixed

- Kafka integration tests now pass on GitLab CI. Redpanda's image defaults to advertising `127.0.0.1:9092`, so clients bootstrapping on the `redpanda:9092` service alias would follow the advertised address back to localhost and fail with `Connection refused`. The `.gitlab-ci.yml` Redpanda service now overrides the startup command to advertise `PLAINTEXT://redpanda:9092`, matching the service alias. GitHub Actions and local docker-compose runs were unaffected (both use a different network topology). (closes #153)

## [0.2.4] - 2026-05-04 _(archive: v2.1.2)_

### Fixed

- Release-please can now create the git tag without blocking on the GitHub Release. Removed `skip-github-release: true` from `release-please-config.json`. The release-please docs note that the flag "should only be used if you have existing infrastructure to tag these releases" — i.e., tag creation is coupled to Release creation in the action. When PR #147 (release 2.1.1) merged with the flag set, release-please saw it as "untagged merged release PR outstanding" and aborted. Backfill required manual `git tag && git push` for v2.1.1.
- `release.yml` now uses explicit `gh release edit` (if Release exists) or `gh release create` (if not) instead of `softprops/action-gh-release@v3`, which only upserts *unpublished* (draft) releases. With release-please creating a published Release on merge, the explicit branching ensures `release.yml` always overwrites the auto-generated commit-summary body with the hand-curated CHANGELOG section.

## [0.2.3] - 2026-05-04 _(archive: v2.1.1)_

### Fixed

- `release.yml` now triggers on tag pushes from release-please. Previously, release-please pushed tags using `GITHUB_TOKEN`, which by GitHub's anti-loop policy does not trigger downstream workflows — so v2.1.0's tag landed on origin but `release.yml` never fired and no GHCR images were built for it. Fix: `release-please.yml` now reads a `RELEASE_PLEASE_TOKEN` secret (PAT with `repo` + `workflow` scope) when present and falls back to `GITHUB_TOKEN`. Tags pushed by a PAT-authenticated workflow do trigger downstream workflows. Until the secret is set, the gap is bridged by `workflow_dispatch` on `release.yml` or by manual delete-and-re-push of the tag from a local clone.
- `release-please` no longer creates the GitHub Release. `skip-github-release: true` in `release-please-config.json` keeps it scoped to "manage version + tag." `release.yml` continues to handle Release creation, reading the matching `[X.Y.Z]` section from `CHANGELOG.md` for the body — preserving editorial control over the release notes.
- `release.yml` gains a `workflow_dispatch` trigger so backfills work without re-tagging. Useful when a tag was pushed without firing the workflow, provided the workflow definition at the tagged ref already includes `workflow_dispatch` (otherwise: delete + re-push the tag from local).

## [0.2.2] - 2026-05-04 _(archive: v2.1.0)_

### Added

- `release-please` automation for version-bumping and tag-pushing. New `.github/workflows/release-please.yml` runs on pushes to `main`; the action maintains a rolling "chore: release vX.Y.Z" PR that accumulates pending changes and computes the next version from Conventional Commits subjects (`feat:` → minor, `fix:` → patch, `BREAKING CHANGE:` → major). Merging the release PR pushes the tag. CHANGELOG remains hand-curated — `release-please-config.json` sets `skip-changelog: true` so the action only manages versioning, never touches the narrative. `docs/reference/versioning.md` updated with the new maintainer flow.

### Fixed

- `release-please-config.json` package path corrected from `iris_core` to `.` (repo root) so commits anywhere in the repo qualify for release computation. Previously release-please was path-filtering commits to only those touching files inside `iris_core/`, missing every change to variants, docs, CI, and root-level config — including the release-please setup PR itself.
- `release-please-config.json` extra-files type corrected from `python` (not a real release-please type) to `toml` with `jsonpath: $.project.version`. The first config used `type: python` which release-please rejected with `unsupported extraFile type: python`. Toml-with-jsonpath is the documented way to point at a non-standard pyproject.toml location.

## [0.2.1] - 2026-05-04 _(archive: v2.0.1)_

### Docs

- New `security-posture.md` Control 11: "Log-stream trust boundary (when Kafka is enabled)." Names Kafka topic ACLs as the trust boundary for log content when `KAFKA_BROKERS` is set, with a per-field breakdown of what the envelope carries (driver text, redacted usernames, `host` from `socket.gethostname()`, `database`, `deployment`, `extra={}` fields). Calls out the hostname leak risk for non-Kubernetes deployments where the host's hostname may be sensitive (`db-server.example.internal`); points operators at `KAFKA_CLIENT_ID` override as a mitigation. (closes #132)
- Cross-reference added in `operating-iris.md` Kafka section pointing SREs at Control 11 for the threat-model framing.

## [0.2.0] - 2026-04-30 _(archive: v2.0.0)_

A second-major-bump release driven by one breaking change (`CONFIG_SOURCE` now required) plus a substantial accumulation of additive features: Kafka log sink, three error-detail modes, three external configuration sources, deployment identity, native TLS, admin-token auth, optional metrics / tracing, a circuit breaker, a query timeout, configurable pool sizing with a startup advisory, a multi-model audit pass that surfaced and fixed three additional bugs (including a security-posture contract gap on username redaction), and a pytest-driven promotion harness for upgrade-in-place validation across all five variants.

**Breaking changes** (one):
- `CONFIG_SOURCE` is now required — no default. Operators must set it explicitly to `local`, `git`, or `db`. Migration: add `CONFIG_SOURCE=local` to your env (`.env.example` files already do this). The validator returns a friendly error pointing at `docs/user-guide/external-configuration.md` when unset. (closes #107)

### Added

#### External configuration

- External configuration via `CONFIG_SOURCE=db`. Per-deployment Postgres database (named after `DEPLOYMENT_NAME`) on a shared config Postgres server — one config DB serves N IRIS deployments without co-mingling. Two well-known tables (`iris_config_validations`, `iris_config_queries`); `CREATE DATABASE` + `CREATE TABLE IF NOT EXISTS` happen on first startup (requires `CREATEDB` grant on the IRIS service account). Devs manage config via SQL; `POST /admin/reload-config` re-queries. Requires the new `iris_core[config-db]` extra (psycopg). Database-per-deployment isolation lets RBAC use Postgres-native `GRANT CONNECT ON DATABASE` rather than row-level filtering. (#99)
- External configuration via `CONFIG_SOURCE=git`. Operators who don't want to fork IRIS to add or change `validation/` and `queries/` YAMLs can point at a config repo: `CONFIG_GIT_URL`, optional `CONFIG_GIT_BRANCH` (default `main`), optional `CONFIG_GIT_TOKEN` for HTTPS-private repos. Lifespan startup shallow-clones the branch via `dulwich` (pure-Python; no system `git` binary needed); the existing loaders read from the cloned tree. New `POST /admin/reload-config` (admin-token-gated) pulls the latest commit and re-runs both loaders so devs can ship YAML changes via PR + merge + curl, no pod restart. Requires the new `iris_core[config-git]` extra. (#98)

#### Deployment identity

- `DEPLOYMENT_NAME` env var — canonical identity for an IRIS deployment. When set, threads through every operator-visible surface where identification matters: structured-log records (`"deployment"` field), OTel `service.name` (defaults to `iris-<name>` when `OTEL_SERVICE_NAME` is unset), `X-Iris-Deployment` response header on every request, `/health` and `/ready` body, `/admin/pool-sizing` JSON, and the OpenAPI title at `/docs` and `/openapi.json`. Validated against Postgres identifier rules (`^[a-z][a-z0-9_]{0,62}$`) so the same name can drive a per-deployment config-DB database name without rename churn. (#97)

#### Error response shaping

- `ERROR_DETAIL=safe` — third mode alongside `terse` (default) and `verbose`. Driver-error response bodies become `{"error": {"code": "db.<class>", "message": "<safe>"}}` so machine consumers can branch on a stable failure class without seeing DB topology in the body. Codes: `db.bad_credentials`, `db.connection_refused`, `db.permission_denied`, `db.timeout`, `db.query_failed` (catch-all). Patterns live in `iris_core/error_classify.py` and match the wrapped driver text case-insensitively. Logs always retain the full driver text regardless of mode. Validation-time errors still collapse to terse strings under `safe`. (closes #82)
- Verbose-mode error response bodies now include `deployment` (when `DEPLOYMENT_NAME` is set) and `database` operator-debug fields. Applies to both validation-time `HTTPException` paths (via a new `iris_core.error_handler.register(app)` exception handler each variant calls during startup) and the `IrisDatabaseError` JSONResponse paths in `routes/inventory.py` and `routes/queries.py` (via `db_error_body()` extension). Terse and safe modes are unchanged — safe deliberately preserves its stable shape for machine consumers; the existing `X-Iris-Deployment` response header (#97) already carries deployment identity for those callers. (closes #101)

#### Logging / observability

- Optional Kafka log-stream sink. Set `KAFKA_BROKERS` and install the new `iris_core[kafka]` extra (`confluent-kafka`); the root logger attaches a queue-buffered `KafkaHandler` that publishes the same JSON envelope going to stdout. **Multi-producer-safe by design** — N IRIS instances can publish to one topic; consumers demux via the `host` (always emitted) and `deployment` envelope fields, and `KAFKA_CLIENT_ID` defaults to `iris-<deployment>-<host>` so broker-side observability doesn't conflate replicas. **Drop-on-failure, never block:** broker outages, queue overflow, producer errors all increment counters and return rather than backpressuring into request handlers; disk-buffer + replay is out of scope. Prometheus gauges (`iris_kafka_records_published_total`, `iris_kafka_records_dropped_queue_full`, `iris_kafka_records_dropped_buffer_full`, `iris_kafka_records_dropped_producer_error`) surface drop visibility when `ENABLE_METRICS=true`. Settings: `KAFKA_BROKERS`, `KAFKA_TOPIC` (default `iris.events`), `KAFKA_CLIENT_ID`, `KAFKA_ACKS` (default `1`, validator rejects unknown), `KAFKA_QUEUE_MAX` (default 10000). The `JSONFormatter` `host` field is always emitted (stdout too) — minor schema addition for downstream consumers. (closes #23)
- Optional OpenTelemetry tracing. Set `ENABLE_TRACING=true` and install the new `iris_core[tracing]` extra to get FastAPI HTTP-layer spans, `traceparent` propagation from upstream gateways, and OTLP gRPC export. Collector endpoint and service name flow through standard OTel env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`). Default off — no SDK initialization, no surprise deps. (#19)
- Optional Prometheus `/metrics` endpoint. Set `ENABLE_METRICS=true` and install the new `iris_core[metrics]` extra to get request count / in-flight gauge / latency histograms / error rate via [`prometheus-fastapi-instrumentator`](https://github.com/trallnag/prometheus-fastapi-instrumentator). Default off — no surprise route, no surprise dep. Endpoint is unauthenticated when enabled; restrict via network policy or mesh. (#16)
- Startup warning for unsafe defaults via new `iris_core/iris_core/startup_warnings.py` module. Each variant's lifespan calls `startup_warnings.report(settings)` once on boot. Today's only check: `MAX_PAGE_SIZE=0` (no result-set cap) emits a WARNING-level log line. The warning lands on stdout and (when enabled) on Kafka, so operators see it once per pod start without having to read the env-var reference. (closes #131)

#### Reliability

- Optional async circuit breaker around DB calls. Set `CIRCUIT_BREAKER_ENABLED=true` (default off) on infrastructure with transient outages to stop every incoming request from blocking on its driver timeout. After `CIRCUIT_BREAKER_FAIL_MAX` consecutive failures (default 5), subsequent requests short-circuit with `503 Retry-After` for `CIRCUIT_BREAKER_RESET_TIMEOUT` seconds (default 5) before allowing a probe. Per-process state — each uvicorn worker sheds load on its own view of the DB. (#18)
- `QUERY_TIMEOUT_SECONDS` env var (default 30s) bounds per-query execution time at the DB layer. Each variant wires its native mechanism: Postgres `statement_timeout` via the pool's `configure` hook, MySQL `MAX_EXECUTION_TIME` via `init_command`, MariaDB `max_statement_time`, Oracle `call_timeout` per connection, Trino `query_max_execution_time` via `session_properties`. `0` disables. Set below your gateway's request timeout. (#17)

#### Pool sizing

- `POOL_MIN_SIZE` and `POOL_MAX_SIZE` env vars expose the connection-pool bounds (defaults unchanged at 2/10). Previously hardcoded in each variant's `db.py`. Trino skipped per design (no real pool concept). (#62)
- Startup pool-sizing report. After the DDL harvest, each pod logs a single INFO event with the DB's observed `max_connections` (or equivalent), the configured pool, the expected peak replica count from `HPA_MAX_REPLICAS` (default 10), and a small recommendation table marking the current `pool_max` and listing two alternatives with verdicts (`ok`/`caution`/`unsafe`). When the DB doesn't expose a limit (Trino, Oracle metadata-only accounts), the report says "unknown" with the reason rather than failing startup. (#63)
- `GET /admin/pool-sizing` returns the same report as JSON. Gated by `X-Admin-Token`. (#63)
- `HPA_MAX_REPLICAS` env var (default 10) feeds the pool-sizing report's peak math. Set to your HPA `maxReplicas` (or static replica count) for accurate advice. Never auto-applied. (#63)

#### Auth and TLS

- `ADMIN_TOKEN` env var and `X-Admin-Token` header. `/admin/*` endpoints now require the header to match `ADMIN_TOKEN` via `hmac.compare_digest`. Independent of the JWT lane (admin actions are typically machine-to-machine — CD pipelines, runbooks). Unset → admin endpoints fail closed with 401 "admin token not configured." (#66, closes #58)
- `GET /queries` listing now requires `X-Admin-Token` (uses the same gate as `/admin/*`). Previously open whenever JWT auth was disabled, leaking operator-authored query names that are useful pre-attack reconnaissance. (#69)
- `allow_writes: true` field on custom-query YAML to opt into non-SELECT statements. (#71)
- Native TLS via `TLS_CERT_FILE` and `TLS_KEY_FILE` env vars. When **both** are set, the new container entrypoint launches uvicorn with `--ssl-certfile` / `--ssl-keyfile` and IRIS terminates TLS itself on port 8000. Default unchanged (HTTP). Half-set (one of the two) fails fast at launch rather than silently falling back to HTTP. (#65)

#### Test infrastructure

- Promotion harness: `test-infra/promotion/`. Pytest-driven; boots an IRIS image at a previous git ref **and** the current working tree against the same data DB and env, then validates both via the same contract checks. Catches the regression class "PR broke upgrade-in-place." All five variants wired (postgres / mysql / maria / ora / trino); each is one entry in `lib.py::VARIANTS`. Skips cleanly without `IRIS_PROMOTION_PREV_REF` set. PREV images are built from a `git worktree` and SHA-tagged so re-runs against the same `PREV_REF` skip the rebuild; CURRENT always rebuilds. Manual harness; CI integration deferred until a tagged baseline exists — this release provides that baseline. (closes #119, #120, #121, #122, #123, #108)
- Real-Kafka integration tests for the Kafka log sink: `iris_core/tests/test_kafka_logging_integration.py`. Skips when `IRIS_TEST_KAFKA_BROKERS` is unset. Single-producer test asserts the full envelope round-trips cleanly. Multi-producer test proves the topology promise: two IRIS-like setups → one topic → consumer demuxes cleanly by `(deployment, host)`. New Redpanda service in `test-infra/docker-compose.yml` (port 9092) and CI service containers in both `.github/workflows/unit-tests.yml` and `.gitlab-ci.yml`. (closes #115)
- e2e + test-infra coverage for `CONFIG_SOURCE=db`. New `iris-config-pg` service in `test-infra/docker-compose.yml` (port 5433); `test-infra/e2e_test.sh` gains a Config-source section that seeds a known custom-query row into each variant's deployment DB and posts to `/admin/reload-config` to verify the loader picks it up — proves all 5 variant images can boot, bootstrap, and serve config from a real Postgres. (closes #106)

#### Tooling and templates

- `.github/workflows/release.yml` — tag-triggered release pipeline. A `vX.Y.Z` push builds all 5 variant images for `linux/amd64` + `linux/arm64`, publishes them to `ghcr.io/baelfur/iris-{variant}:X.Y.Z` (also `:latest` for stable releases), and creates a GitHub Release whose body is the matching `CHANGELOG.md` section. Pre-release tags (`-rc.N`, `-beta.N`) skip the `:latest` update. (#38)
- All five `<variant>/.env.example` files gain commented placeholders for the production-recommended env vars (`MAX_PAGE_SIZE`, `ADMIN_TOKEN`, `LOG_USER_SECRET`, `DEPLOYMENT_NAME`, `ERROR_DETAIL`). Each placeholder carries a one-line explanation of when to set it. (closes #131)

### Changed

- **Breaking:** `CONFIG_SOURCE` is now required — see top-of-section breaking-change callout. (closes #107)
- `ALLOWED_SCHEMAS` is now optional. Default (unset) → harvest every non-system schema the DB service account has `SELECT` on; the per-variant `SYSTEM_SCHEMAS` constant excludes catalogs like `pg_catalog` / `information_schema` / `SYS`. Set → narrowing override on top of grants. The lifespan `RuntimeError` on empty allowlist is gone — the canonical security boundary is the DB's role grants, with `ALLOWED_SCHEMAS` as an optional second layer. `docs/user-guide/security-posture.md` Control 2 reframes around DB grants; `docs/user-guide/operating-iris.md` includes a "When to set `ALLOWED_SCHEMAS`" section. (closes #64)
- Service-account username is now redacted from driver-error text in logs and Kafka envelopes. Pre-fix, redaction only ran on the passthrough lane (`X-DB-Authorization` username); when the pool used the configured service account and the driver echoed it on a permission error, the username landed unredacted in logs. Each variant now wires `db_user` into `IrisContext` from its DB-specific user setting; routes redact `ctx.db_user` from driver-error detail in addition to the optional passthrough caller. New `iris_core/tests/test_redact_integration.py` exercises both lanes against a route-level FastAPI TestClient. `docs/user-guide/security-posture.md` Control 8 reframes around both username sources. Caught by the GPT-5 Codex blind audit (highest-severity finding). (closes #129)
- **`ERROR_DETAIL` default is now `terse`** (was `verbose`). Response bodies collapse to generic strings by default; specific text is opt-in via `ERROR_DETAIL=verbose` for dev / trusted-network use. Logs always retain the full driver text. (#70)
- DDL-harvest queries in every variant now flow `ALLOWED_SCHEMAS` through bind parameters instead of f-string interpolation. Not a security fix (it's operator-controlled config, never request input), but every SQL emission path is now bind-parameterized. (#73)
- Custom-query YAML loader skips files whose `sql` doesn't begin with `SELECT` or `WITH` (CTE) and logs an ERROR pointing operators at the new `allow_writes: true` opt-in. Defense-in-depth on the operator-authored surface. Leading whitespace and SQL comments are tolerated before the SELECT. (#71)
- Internal refactor: paramstyle bind emission consolidated into `iris_core.paramstyle.BindAccumulator`. The expression parser, simple-filter branch in `build_query`, and custom-query `_substitute_params` all share one helper instead of three near-duplicate paramstyle if/elif/else blocks. No env-var or response-shape change. (#42)
- Migrated `iris_core.base_settings` off the Pydantic V1 `class Config` pattern to `SettingsConfigDict`. Silences the per-startup deprecation warning and removes the V3-removal landmine. (#44)
- CI hygiene pass: GH Actions paths-filtered so docs-only / k8s-only / Dockerfile-only PRs don't trigger the 12-job test matrix; `.gitlab-ci.yml` mirrors with `changes:` rules. All GH Actions bumped to current Node-24-compatible major versions ahead of GitHub's June 2026 Node 20 deprecation. Pytest's `asyncio_default_fixture_loop_scope` pinned to `function` to silence the deprecation warning. (#88, #49, #45 partial)

### Fixed

- `setup_logging()` now preserves the Kafka queue handler when called a second time. Pre-fix, a re-init would `logging.root.handlers = [stdout]` (wiping the queue handler), then `kafka_logging.maybe_attach()` would early-return because `_listener is not None` — silently disabling Kafka export with no error or log line. Now `maybe_attach()` re-attaches the existing queue handler from a module-level reference. Caught by all three reviewers in the 2026-04-29 multi-model audit. (closes #127)
- README Hello IRIS walkthrough and `operating-iris.md` no longer claim `CONFIG_SOURCE=local` is the default. (closes #128)
- `$select` and `$orderby` now rebuild their SQL clauses from validated tokens instead of interpolating the raw query-string value. Previously `parse_column_list` only validated the first whitespace-token of each comma-chunk; trailing content rode into the emitted SQL. (#68)
- View-definition `required` params can no longer be bypassed by sending `$filter` instead of the simple-filter form. (#59)
- `build_links` now preserves simple `?col=val` filters in the next-page URL. Pagination over a filtered query was silently broadening the result set. (#60)
- The queries router is now registered before the inventory catch-all in every variant. Single-segment custom queries (`queries/foo.yaml` → `/queries/foo`) were unreachable because the 2-segment URL matched `/{schema}/{view_name}` first. (#72)
- Postgres passthrough no longer builds its libpq conninfo by string-format. Credentials flow as kwargs to `psycopg.AsyncConnection.connect` so a passthrough caller embedding libpq keywords in their username (`alice dbname=other_db`) cannot redirect the connection. (#67)
- Custom-query bind keys mirror the operator's param name verbatim. Previously the substitution synthesized `p_{name}` keys; an operator who wrote a literal `:p_x` token in YAML SQL alongside a `:x` placeholder could see the literal silently treated as a duplicate placeholder. The collision class is gone. (#74)
- `k8s/secret.yaml` for `postgres/`, `mysql/`, `maria/`, and `ora/` now carries each variant's actual env vars and `ALLOWED_SCHEMAS`. Previously the Postgres / MySQL / MariaDB files shipped with Oracle env vars (copy-paste artifact); four of five lacked `ALLOWED_SCHEMAS` so the pod RuntimeError'd at startup. (#57)
- `test-infra/e2e_test.sh` passthrough tests use `X-DB-Authorization` rather than `curl -u` (which sends `Authorization`, ignored by IRIS for the passthrough path). The previous tests passed for the wrong reason — bad creds didn't actually reach the DB. (#61)

### Removed

- `docs/query-api.md`, `docs/security.md`, `docs/deployment.md` — replaced by `docs/user-guide/{using-iris,security-posture,operating-iris}.md`. The user-facing content was rewritten with audience-first framing; technical reference moved into `docs/reference/architecture.md`.
- `docs/migration-v1.md` and `docs/legacy-comparison.md` — internal-context content that didn't belong in shipped docs. Preserved locally in `.claude/parked-docs/`.

### Docs

- New `docs/user-guide/external-configuration.md` page covering the three config-source patterns end-to-end: when to pick which, setup and auth per source, dev → UAT → prod promotion mechanics, migration recipes (`local` → `git` and `local` → `db`), Shape A vs Shape B adoption shapes, forward-compatibility contract, trust-boundary summary, and per-source troubleshooting. (#104)
- **Docs restructured** into `docs/user-guide/` (audience-oriented prose for integrators, operators, security reviewers, and adopters) and `docs/reference/` (technical specs for maintainers and contributors). New `docs/user-guide/when-to-use.md` is a decision guide that didn't exist before. (#54)
- `docs/user-guide/security-posture.md` and `docs/user-guide/operating-iris.md` document the JWKS-fetch dependency: when `AUTH_JWKS_URL` is set, JWT validation depends on the IDP being reachable at request time. (#75)
- `docs/user-guide/security-posture.md` updates after adversarial review: scoped the "writes are not reachable" claim to the dynamic surface; listed `/health`, `/ready`, `/readyz`, and `/queries`-when-JWT-disabled as unauthenticated alongside `/admin/refresh-schema`; noted that verbose-mode error responses leak hostnames / ports / DB names; added the YAML write surface as an explicit threat-model item.

## [0.1.0] - 2026-04-23 _(archive: v1.0.0)_

First stable release. Grammar and security model converge into a single "safe by construction" shape. The `LEGACY_FILTER_COMPAT` shim lets existing callers keep working during the cutover.

**Breaking changes** are explicitly marked below. Summary:
- `$filter` / `$having` grammar rewritten — legacy SQL-fragment syntax (`=`, `AND`, `LIKE`, function calls) is no longer accepted at the dynamic surface. Use the new operator tokens, or enable the compatibility shim during migration.
- `SECURITY_MODE` env var removed. Behavior is uniform; deployments with it set still start cleanly.
- `build_query()` signature change — internal only; variant adapters unaffected.

### Added
- `iris_core/expression.py` — closed-grammar parser for `$filter`. Tokens (`eq`/`ne`/`gt`/`ge`/`lt`/`le`/`and`/`or`/`not`/`in`), precedence, identifier validation against DDL cache, parameterized literals across all three paramstyles. (#31)
- `$groupby` — comma-separated column list emitted as `GROUP BY`. Identifiers validated against the DDL cache. Requires an explicit `$select` whose columns are all present in `$groupby` (matches a common semantic; prevents accidental non-aggregate misuse). (#32)
- `$having` — expression using the same closed grammar as `$filter`, emitted as `HAVING`. Only valid when `$groupby` is present. Filter and having share the bind counter — binds interleave as `f0`, `f1`, … across both clauses. (#33)
- `iris_core/tests/` — consolidated unit suite for everything that doesn't vary by paramstyle (parser, schema cache, view defs, readiness, creds, redaction). Runs once in CI under a new `iris-core` job. Each variant's `tests/test_unit.py` now contains only paramstyle-specific SQL-emission coverage. `iris_core[test]` extras install pytest + pytest-asyncio for the shared suite. (#41)
- `iris_core.integration_test_suite` — shared async integration-test bodies (34 tests). Each variant's `tests/test_integration.py` now imports `*` from this module; per-variant context (URL paths, passthrough credentials, the ASGI client, data seeding) lives in each variant's new `tests/conftest.py`. Trino's passthrough fixtures return None so the two passthrough tests skip automatically instead of being omitted from the file. (#47)
- `iris_core.migrate_filter` — opt-in compatibility shim that rewrites legacy SQL-fragment `$filter` / `$having` syntax to the v1.0 closed grammar at request time. Enabled via `LEGACY_FILTER_COMPAT=true`. Handles operator/keyword translation (`=`→`eq`, `AND`→`and`, `IS NULL`→`eq null`, `X NOT IN`→`not X in`, …), preserves string literals verbatim, rejects `LIKE`/`BETWEEN`/function calls with a 400 and rewrite hint pointing at custom queries. Frozen substitution table — broadening it would undermine the closed-grammar safety guarantee. (#36)
- `LEGACY_FILTER_COMPAT` env var (default `false`) — toggles the shim above. Running the shim over already-v1.0 input is a no-op (only legacy tokens match), so deployments can leave it on indefinitely during a transition.
- Migration guide for legacy callers — TL;DR, operator translation table, unsupported-construct reference with custom-query guidance, shim-enablement steps, env-var delta, "why the break" section. (#36)

### Changed
- **Breaking:** `$filter` grammar replaced with the closed expression language. Legacy SQL-style operators (`=`, `<>`, `AND`, `OR`, `LIKE`, `IS NULL`, `BETWEEN`, arbitrary functions) are no longer accepted. Use `eq`/`ne`/`and`/`or`/`eq null`, and route complex expressions through custom queries at `/queries/*`. See epic #30 for rationale and the migration table. (#31)
- **Breaking:** `build_query()` signature now takes `(schema, view_name, params, paramstyle=...)` — the table name is constructed internally (still uppercase for Oracle, lowercase elsewhere). Only `iris_core.routes.inventory` called this; variant adapters are unaffected.
- Filter literals now flow through bind parameters keyed `f0`, `f1`, … (expression-parser scope), distinct from the simple-filter `p_{col}` keys. No SQL injection surface in `$filter` by construction.
- **Breaking:** all security modes collapsed into one behavior. `$filter`/`$having` are always accepted and parsed by the closed grammar; DDL validation always runs. `SECURITY_MODE` env var is removed — deployments that set it still start (Pydantic's `extra = "ignore"` tolerates unknown env vars) but the value has no effect. Behavior matches the former `sorta-secure` post-#31: closed-grammar `$filter`, DDL-validated identifiers. (#34)

### Removed
- `schema_cache.validate_filter` and the `BLOCKED_PATTERNS` regex blocklist — obsolete once `$filter` is a closed grammar.
- `_translate_filter` helper in `query_engine` — replaced by the full parser.
- `IrisBaseSettings.security_mode` field, the `SECURITY_MODE` env var, and all mode-specific branches in `iris_core/routes/inventory.py` and variant `app/main.py` files. (#34)
- `trino/.env.example` and `trino/k8s/secret.yaml` no longer reference `SECURITY_MODE`. (#34)

### Docs
- `docs/security.md`: security-modes section removed; unified "Query-time validation" section covers DDL validation + safe params + closed grammar. `docs/deployment.md` drops `SECURITY_MODE` from the env reference. `docs/query-api.md` / `docs/legacy-comparison.md` / `docs/versioning.md`: mode references scrubbed. `trino/README.md` reduced to a pointer file (root README + `docs/` are the source of truth post-restructure). (#34)

## [0.0.1] - 2026-04-22 _(archive: v0.1.0)_

Initial release.

### Added

#### Core
- FastAPI + async database driver scaffold
- Dynamic `/{schema}/{table}` routing — mirrors database structure
- `$select`, `$filter`, `$orderby`, `$count`, `$start_index` query parameters
- Safe `?column=value` bind-parameterized queries (implicit AND)
- JSON response envelope: `{"name", "elements", "links"}`
- Pagination with `next` links
- Universal per-request credential passthrough via `X-DB-Authorization` across all 5 variants (#22)
- Real readiness probe that pings the database at `/ready` (#15)

#### Database Variants
- **Oracle** (`ora/`) — `oracledb` (official)
- **PostgreSQL** (`postgres/`) — `psycopg` + `psycopg-pool` (official)
- **MySQL** (`mysql/`) — `aiomysql` (community, async)
- **MariaDB** (`maria/`) — `aiomysql` (community, async)
- **Trino** (`trino/`) — `aiotrino` (community, async)

#### Security
- Three security modes: `mostly-secure` (default), `sorta-secure`, `hold-my-beer`
- DDL schema harvest on startup — validates schema, table, and column names
- Pattern blocking on `$filter` (semicolons, UNION, DROP, etc.)
- YAML view definitions (`validation/`) — required/optional param enforcement
- Optional JWT/OIDC Bearer token auth via `AUTH_JWKS_URL`
- `ALLOWED_SCHEMAS` required — no default, fails fast if unconfigured
- `ERROR_DETAIL` (verbose/terse) — controls error response information leakage
- `MAX_PAGE_SIZE` — caps row count, enforces pagination
- HMAC-based username redaction in logs for passthrough traffic (`LOG_USER_SECRET`)

#### Custom Queries
- YAML files in `queries/` define SQL endpoints with parameter contracts
- Bind-parameterized execution, same safety as simple params
- `GET /queries` lists all available custom queries

#### Infrastructure
- Dockerized — each variant has its own Dockerfile
- Kubernetes manifests (deployment, service, HPA, secret) per variant
- Structured JSON logging with `database` field for Datadog aggregation
- `/health` endpoint for liveness probes (never touches DB)
- `/ready` / `/readyz` endpoint for readiness probes (pings DB)
- `POST /admin/refresh-schema` — re-harvest DDL without restart
- GitHub Actions + GitLab CI unit-test pipelines (3.11, 3.12 matrix)

#### Architecture
- `iris_core/` shared package — routes, auth, validation, logging, query engine
- `IrisContext` dependency-injection pattern so `iris_core` imports no variant
- Paramstyle-aware SQL emission: `pyformat`, `named`, `qmark`
- Each variant is 3 files: `config.py`, `db.py`, `main.py`
- Adding a new database = create a directory with those 3 files

#### Testing
- Unit + integration test suites per variant
- `test-infra/` — docker-compose with seeded Oracle, PostgreSQL, MySQL, MariaDB, Trino
- `test-infra/smoke_test.sh` — container-level HTTP smoke tests

#### Documentation
- Audience-oriented `docs/` tree: query-api, security, deployment, architecture, variants, legacy-comparison (#29)

### Fixed

- #1: Remove bare `pass` in exception handlers
- #4: Normalize schema/table case in SQL construction
- #5: Replace naive string substitution with word-boundary regex in custom queries
- #6: Fix Oracle error log messages appearing in non-Oracle variants
- #7: Fail fast on startup if `ALLOWED_SCHEMAS` is empty
- #11: Trino unit tests no longer require database connection
- #12: Trino connection pooling added
- #14: Trino README added

[Unreleased]: https://github.com/Baelfur/iris/compare/v2.1.1...HEAD
[2.1.1]: https://github.com/Baelfur/iris/releases/tag/v2.1.1
[2.1.0]: https://github.com/Baelfur/iris/releases/tag/v2.1.0
[2.0.1]: https://github.com/Baelfur/iris/releases/tag/v2.0.1
[2.0.0]: https://github.com/Baelfur/iris/releases/tag/v2.0.0
[1.0.0]: https://github.com/Baelfur/iris/releases/tag/v1.0.0
[0.1.0]: https://github.com/Baelfur/iris/releases/tag/v0.1.0
