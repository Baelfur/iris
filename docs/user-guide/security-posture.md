# Security posture

This is the summary for someone reviewing IRIS before it's introduced to the stack. It describes the threat model, the trust assumptions IRIS makes, the controls in place, and the classes of risk that are outside IRIS's scope and live elsewhere in your environment.

## Summary

IRIS is a read-only HTTP façade in front of a relational database — the dynamic surface neither writes nor executes arbitrary SQL submitted by callers. Every identifier in every request is validated against a live DDL cache; every literal is bound as a driver-native parameter. The URL filter grammar is a closed language that can express comparisons, `in` lists, nulls, and boolean combinators — nothing else.

Operator-authored custom SQL (`queries/*.yaml`) is permitted and encouraged for anything the closed grammar can't express. This is a review-gated surface: operators write the SQL, consumers only parameterize it. See the two-surface model below — they have different safety properties, and conflating them undermines the dynamic surface's structural guarantees.

## Two surfaces, two safety models

IRIS exposes two query surfaces with deliberately different safety properties. Understanding the split is the foundation of the security model:

| Surface | Audience | Grammar | Safety model |
|---|---|---|---|
| `/{schema}/{table}` | End users (apps, services) | Closed expression grammar | **Safe by construction** — closed operator set, identifiers validated against the DDL cache, literals always bind-parameterized |
| `/queries/{name}` | Operator-authored YAML | Arbitrary SQL | **Safe by review** — every expression is committed to version control and reviewed at author time |

The split is intentional:

- Dynamic endpoints give end users predictable read access without letting them shape the SQL. The grammar is narrow on purpose — no functions, no subqueries, no arbitrary expressions. Proposals to extend it for flexibility should be redirected to custom queries.
- Custom queries exist for everything the dynamic endpoints don't allow: joins, aggregates, `CASE`, domain-specific functions. Because the SQL is operator-authored and committed, DBAs and reviewers see every expression that will ever hit the database before it ships.

Conflating the two erodes the structural-safety property of the dynamic surface, so it isn't done.

These are categorically different kinds of safety. "Safe by construction" is a structural impossibility — the parser can't express a write or a function call, so no input produces one. "Safe by review" is a process control — it depends on operator discipline at commit time and pipeline integrity. Either is fine for the surface it covers; treating them as interchangeable is the failure mode.

## Threat model

### In scope

1. **Injection via URL parameters.** A caller attempts to extend or reshape SQL by smuggling SQL fragments through `$filter`, `$having`, simple `?col=value` filters, `$select`, `$orderby`, or `$groupby`.

2. **Unauthorized schema/table access.** A caller attempts to read from a schema that wasn't supposed to be exposed, or a table they shouldn't see.

3. **Exfiltration of unrelated data.** A caller attempts to join, union, or subquery into tables outside the intended read set.

4. **Resource exhaustion.** A caller requests very large page sizes to consume memory and pool workers.

5. **Credential leakage in logs.** DB error messages can contain usernames the database has echoed back.

6. **Operator-authored SQL.** Custom queries in `queries/*.yaml` execute whatever SQL the operator wrote, with whatever privileges the service account has. The threat here isn't a hostile end user — they can't add a YAML file — but the merge pipeline that lands files in `queries/` is a write surface in its own right. Treat YAML changes like SQL migrations.

### Explicitly out of scope — handled elsewhere

- **Authentication at the HTTP edge in production.** IRIS has optional JWT validation; it does not have API keys, OAuth flows, or session cookies. The expectation is that IRIS runs behind an API gateway or service mesh that terminates whatever auth scheme the organization uses, and IRIS sees validated traffic. For interactive SSO (browser login, Swagger UI Authorize button), the recommended pattern is a reverse proxy in front of IRIS — `oauth2-proxy`, `pomerium`, cloud-vendor IAP, or ingress-controller plugins all work. See [operating.md "Putting SSO in front of IRIS"](operating.md#putting-sso-in-front-of-iris) for the pattern + an example sidecar config.

- **Rate limiting.** There is none in IRIS itself. Bring your own at the gateway.

- **Row-level / column-level authorization.** IRIS decides "this table is allowed" at a coarse grain. Finer-grained authorization belongs either in the database (via the credential-passthrough lane, which lets the DB's native permissions apply) or in the gateway (via policy).

- **TLS termination.** IRIS listens HTTP by default in the stock manifests. Run it behind a TLS terminator (ingress / mesh / sidecar). For deployments where that's not available, IRIS will terminate TLS itself when both `TLS_CERT_FILE` and `TLS_KEY_FILE` are set — see [operating.md "Getting TLS in front of IRIS"](operating.md#getting-tls-in-front-of-iris). Cert rotation, mTLS, and ACME automation remain out of scope; pick the terminator pattern for those.

- **Network segmentation.** Whether IRIS can reach the DB, and whether callers can reach IRIS, is a k8s / VPC / network-policy concern.

- **Secret management.** The stock manifests have a `secret.yaml` stub with placeholders. Replace it with something that integrates with your secret store (external-secrets, sealed-secrets, Vault, cloud KMS-backed secrets).

## Controls

### 1. The dynamic surface cannot reach writes

`GET /{schema}/{table}` and its query parameters can never produce a non-`SELECT` statement. SQL generation goes through one builder that only ever emits `SELECT ... FROM ... WHERE ... GROUP BY ... HAVING ... ORDER BY ... LIMIT/OFFSET`. There is no "mode" that enables writes; this is a property of the code, not a configuration.

The `/queries/*` custom-SQL surface is different — it executes whatever the YAML file says. The loader enforces SELECT-only by default via a two-stage heuristic check: the SQL must begin with `SELECT` or `WITH` (after stripping comments + whitespace), AND the body must not contain a data-modification keyword (`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `DROP`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`, `REPLACE`, `CALL`, `EXEC`) as a whole word (comments and string literals are stripped before the scan to avoid false positives). The two-stage shape exists to catch writable-CTE patterns like `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x` that would otherwise pass a leading-token check. A YAML failing either stage is skipped at load time with a loud ERROR log. Operators who genuinely need a write query opt in with `allow_writes: true` — that's the explicit handshake.

**Important: the loader check is a smell-test, not a security boundary.** It catches operator mistakes (typoed `DELETE`) and the common writable-CTE bypass, but heuristic keyword scans aren't a SQL parser and SQL is large. **The real boundary is the DB grant the service account holds.** Operators running custom queries should run IRIS under a `metadata_user`-style read-only role and use `X-DB-Authorization` passthrough for any caller who needs write access; the loader check is the second layer, not the first.

Verify the dynamic surface by checking the route definitions and the query-builder module. Verify the YAML surface the same way you verify SQL migrations (review, role grants, pipeline integrity), with extra scrutiny on any file that sets `allow_writes: true`.

### 2. Identifiers are validated, never concatenated from request input

At startup, IRIS queries the database's information schema (or equivalent) and builds a map: `{schema: {table: {column, column, ...}}}`. This is the DDL cache.

Every request identifier — schema name, table name, every column referenced in `$select`, `$filter`, `$orderby`, `$groupby`, `$having`, and simple `?col=value` filters — is checked against this cache before any SQL is built. A schema not in the cache → 404. A table not in the cache for that schema → 404. A column not in the cache for that table → 400.

What ends up *in* that cache is determined by the configured scope:

- **Default (no `allowlist.yaml`):** the harvest excludes a per-database list of system schemas (e.g. `pg_catalog`, `information_schema` on Postgres; `SYS`, `SYSTEM`, `XDB`, ... on Oracle). Whatever's left that the IRIS service account has `SELECT` on becomes part of the dynamic surface. The DB's role grants are the canonical scope.
- **`allowlist.yaml` supplied** (`schemas:` and/or `tables:` sections, both glob-pattern-aware): narrows the harvest further. Useful when one service account is shared across multiple deployments, when the operator can't easily request a tightly-scoped service account from the DBA team, or as belt-and-braces in dev/UAT. See [Operating IRIS](operating.md) for the file shape and reload semantics.

  The **`ALLOWLIST__MODE`** setting controls whether the allowlist is a security boundary or a presentation filter:

  - **`enforce` (default).** Non-listed schemas/tables are dropped from the DDL cache; identifier validation returns 404 on any request that references them. The allowlist is a security boundary that composes with the DB's grants.
  - **`presentation`.** The allowlist is applied **only** at OpenAPI render time — non-listed schemas/tables are absent from the spec but **remain reachable at runtime**. Use this mode when an operator wants a curated docs surface (e.g., 50 named tables on a 1000-table estate) without losing direct-URL access for power users. **`presentation` is not a security boundary** — it's a discoverability filter, and a startup WARNING surfaces the trade. Operators relying on the allowlist to gate access must use `enforce`.

Two consequences worth calling out:

- **The DB's grants are the load-bearing security boundary.** A schema the service account can't `SELECT` from never appears in the cache, regardless of the allowlist. A schema the service account *can* read but the operator chose to exclude via `allowlist.yaml` also never appears. The cache is the intersection: anything reachable in IRIS satisfies *both* the DB's grant AND (when set) the operator's allowlist.

- **Adding a table or column requires a re-harvest before callers can see it.** This is operationally a minor friction, but it's also a security property: DDL changes are a visibility step, not an instant access change.

### 3. Literals are bound, not interpolated

The `$filter` and `$having` expression parser produces an AST. Identifiers flow through identifier-validation. Literals (strings, numbers, null) flow through an emitter that appends them to a paramstyle-appropriate bind container (dict for pyformat/named, list for qmark) and returns placeholder syntax (`%(f0)s` / `:f0` / `?`). Literals are never interpolated into the SQL text.

Simple `?col=value` filters use the same binding path, with bind keys prefixed `p_{col}` to stay disjoint from the expression parser's `f0`, `f1`, ... keys.

Custom queries declared in YAML use named placeholders (`WHERE category = :category`); at request time, IRIS translates each declared parameter into a driver-native bind so caller-supplied values never enter the SQL text directly. The current substitution implementation is regex-based and is being tightened to be SQL-aware so placeholder rewriting can't reach into string literals, comments, or dialect operators — until that lands, operators authoring custom SQL should avoid `:foo`-style sequences inside quoted strings and `::cast` operators with declared-param suffixes.

### 4. The filter grammar is closed

The grammar for `$filter` and `$having` is explicitly and intentionally small. It accepts:

- Comparisons: `ident (eq|ne|gt|ge|lt|le) literal`
- `ident eq null` / `ident ne null` (maps to `IS NULL` / `IS NOT NULL`)
- `ident in (literal, literal, ...)`
- Boolean combination with `and`, `or`, `not` and parentheses

It does not support functions, subqueries, `LIKE`, `BETWEEN`, column-to-column comparisons, or boolean literals. These aren't "disabled" — they can't be expressed. The parser rejects them with a 400 before any SQL is built. Integration tests affirmatively verify that attempts like `id=1; DROP TABLE products` and `trim(name) eq 'x'` are rejected.

Anything that needs a function or pattern match is a custom query — i.e. an operator-reviewed YAML file — not a consumer concern.

### 5. `$select` and `$orderby` validation

Both parameters split on commas, validate every column identifier against the DDL cache, and rebuild the SQL clause from those validated tokens — the raw query-string value never reaches the emitted SQL. `$orderby` additionally accepts an optional `ASC`/`DESC` per column and rejects anything else. A payload like `$orderby=id, name; SELECT pg_sleep(10)` fails the trailing-token check and returns 400 before any SQL is built.

### 6. Credential separation

IRIS accepts three credential lanes, each on its own header:

- `Authorization: Bearer <jwt>` — user-facing API auth. Validated against a JWKS. Answers: "are you allowed to call this API?" Applies to `/{schema}/{table}` and `/queries/*`.
- `X-DB-Authorization: Basic <base64>` — database identity for passthrough. Used only when present; not required. Answers: "whose identity should the SQL run as?"
- `X-Admin-Token: <secret>` — operator credential for `/admin/*`. Compared against `AUTH__ADMIN_TOKEN` with `hmac.compare_digest`. Independent of the JWT lane because admin actions are typically machine-to-machine (CD pipelines, runbooks, cron) without a meaningful end-user identity.

The lanes are independent by design — putting different credential types on the same header collides badly. A caller can have a valid JWT but no DB credentials (uses the service account), valid DB credentials but no JWT (allowed if JWT auth is disabled), both, or neither. Admin endpoints ignore both of those and look only at `X-Admin-Token`.

When passthrough credentials are supplied and wrong, the DB rejects them and IRIS surfaces the error as a 400 — it does not fall back to the service account. This prevents silent privilege upgrade.

`AUTH__ADMIN_TOKEN` unset means admin endpoints fail closed with 401 "admin token not configured." There is no "admin endpoints reachable to anyone" default state.

### 7. Username redaction in logs

DB error messages that include usernames are passed through a redaction step before being logged. With `LOG_USER_SECRET` set, usernames become `user:<16-hex>` (salted HMAC, stable under the secret). Without it, usernames become `<redacted>`.

Both username sources are redacted on the error path:

- The **passthrough caller** (`X-DB-Authorization` username) when the request used credential passthrough.
- The **configured service-account username** that IRIS itself uses for the pool — driver text often echoes it on permission errors (`permission denied for user "metadata_user"`). Redacted regardless of whether the request was passthrough or pool-routed.

This keeps PII out of log aggregators while preserving operators' ability to correlate auth failures for the same user across pods. A log entry can be matched against a candidate username by anyone holding `LOG_USER_SECRET` and running the same hash function locally.

### 8. YAML-declared parameter whitelists

Operators can drop a YAML file into `validation/` to constrain what parameters a table endpoint accepts (`required: [id]`, `optional: [status, city]`). When a file is present for a table, only the listed parameters are accepted; anything else returns 400. This is how you prevent full-table dumps from exposing columns you'd rather callers didn't scan by — e.g. `products` might be constrained to require `id`, so callers can look up individual products but can't iterate all products by sweeping `price>0`.

Custom queries have the same mechanism — required and optional parameters are declared per query.

### 9. `ERROR_DETAIL` is terse by default

Error response bodies collapse to generic strings (`"Bad request"`, `"Not found"`, `"Query failed"`) by default. The threat-model lever is information disclosure: verbose responses leak column names (probing the schema), DB-driver text (hostnames, ports, DB names, role names, constraint identifiers), and the specific reason for each rejection.

Three modes:

| Mode | Body shape on driver error | Use when |
|---|---|---|
| `terse` (default) | `{"error": {"code": "db.query_failed", "message": "Query failed"}}` | Default. Untrusted callers, public-ish endpoints. Caller learns nothing about why. |
| `safe` | `{"error": {"code": "db.<class>", "message": "<safe>"}}` | Caller is a service that wants to branch on failure class (retry on `db.connection_refused`, surface auth failure to its own user, etc.) but the body still can't disclose topology. |
| `verbose` | `{"error": {"code": "db.<class>", "message": "<raw>"}, "deployment": "...", "database": "..."}` | Dev / inside-trust-perimeter only. Returns raw driver text plus operator-debug context (`deployment` when `DEPLOYMENT_NAME` is set, `database` always). Leaks hostnames, ports, DB names, role names. |

**Scope.** All three modes shape only **wrapped DB-driver errors** — failures that round-trip through the database driver. Validation errors that fail before any SQL is built (invalid column, unknown schema, missing required parameter, expression-grammar errors) continue to echo full detail in every mode. They're feedback about the caller's own input, not disclosures about server state, and collapsing them to opaque strings would make every 4xx undebuggable from the client side. The information-disclosure threat targeted by this control is the DB-side topology that an attacker can't see directly; client-input echoes don't carry that information.

All error responses use a unified envelope: `{"error": {"code": "...", "message": "..."}}`. Validation/auth codes derive from HTTP status (`validation.bad_request`, `validation.not_found`, `auth.unauthorized`, `auth.forbidden`); driver codes are `db.<class>` from the classifier. Clients branch on `error.code` rather than on which key is present.

**Codes are an open list.** Today's driver set is `db.bad_credentials`, `db.connection_refused`, `db.permission_denied`, `db.timeout`, and `db.query_failed` (catch-all). New variants or new failure shapes may add codes over time (e.g. `db.deadlock`, `db.disk_full`); the contract for clients is to treat unknown `db.*` codes as `db.query_failed`-equivalent for fallback purposes. Renaming or removing a code is breaking; adding one is not. New patterns are added in `core.errors.classify`. The codes form a fleet-wide vocabulary — any IRIS instance, any variant, emits the same code for the same shape of failure.

Logs always retain the full driver text (with usernames HMAC-redacted per control 7) regardless of mode. Operators get visibility for incident response; clients don't get the topology. The asymmetry is deliberate.

Pick `safe` when you have machine consumers; pick `terse` when you don't; pick `verbose` only behind the trust boundary.

### 10. Log-stream trust boundary (when Kafka is enabled)

When `KAFKA__BROKERS` is set, the same JSON envelope going to stdout also lands on the configured Kafka topic. **Kafka topic ACLs become the trust boundary for log content** — anyone with read access to the topic sees what's in the envelope, regardless of `ERROR_DETAIL` setting on the HTTP surface.

The envelope carries:

- The full driver-error text (host, port, role names, constraint identifiers can all appear)
- Usernames — HMAC-redacted per control 7 (passthrough caller AND configured service account)
- `host` field, populated from `socket.gethostname()`. On Kubernetes pods that's the pod name (non-sensitive). On a bare host or VM with a meaningful hostname (`db-server.example.internal`), that name lands on every record AND in the broker-side `client.id`.
- `database` (variant tag), `deployment` (when `DEPLOYMENT_NAME` is set), and any `extra={}` fields from per-call structured logging.

Operator action: treat the Kafka topic's read ACL as the population that's allowed to see your DB driver text and infrastructure hostnames. The redaction in control 7 reduces the username surface, but everything else is verbatim by design — the same posture stdout-then-aggregator deployments live with, just shifted to a different sink.

If the deployment runs outside Kubernetes and the host's hostname is sensitive, override `KAFKA__CLIENT_ID` (avoids leaking it to the broker side) and consider whether the per-record `host` field meets your deployment's requirements before enabling Kafka.

## Supply chain

Beyond the runtime controls above, IRIS has a small set of CI-side controls that target the dependency surface itself. They run on every push and PR to the `dev` integration branch, so issues surface during integration rather than at release time.

- **`pip-audit --strict`** runs in `unit-tests.yml` against the full optional-extra surface (`[test,metrics,tracing,config-git,config-db,kafka]`). Fails the build on any unfixed CVE in a pinned dependency. This is the independent CVE check; it doesn't depend on Dependabot or any external service catching the advisory first.

- **Dependabot, security-only.** `.github/dependabot.yml` watches `pip` (core/ + 5 variants) and `github-actions` ecosystems weekly, but `open-pull-requests-limit: 0` suppresses routine version-drift PRs. GitHub still raises Dependabot PRs for advisories that affect a pinned dependency — those land against `dev` for review and merge. The reasoning: floor pins in `pyproject.toml` and the variant `requirements.txt` files match the API surface IRIS uses, so chasing major-version bumps for their own sake is busywork; advisory-driven bumps are the only ones with a real "do this now" signal.

- **Optional auto-merge for Dependabot security PRs.** `.github/workflows/dependabot-auto-merge.yml` calls `gh pr merge --auto --squash` on Dependabot PRs so they merge once required checks pass. Gated by the repo variable `DEPENDABOT_AUTO_MERGE` — workflow runs on every Dependabot PR but skips the merge step unless the variable is `"true"`. Default off; flip in **Settings → Secrets and variables → Actions → Variables** when ready. Pairs with branch protection requiring CI green before merge — without that, `--auto` fires the moment GitHub considers the PR mergeable.

The combination gives operators continuous CVE coverage without ongoing version-drift noise: drift-only PRs are off, advisories surface as PRs against `dev`, and (when enabled) merge automatically when CI is green.

## Residual risks and how to handle them

### Unauthenticated endpoints

The deployment model assumes IRIS sits behind a gateway that handles edge auth. The endpoints that are open at the IRIS layer regardless of `AUTH__JWKS_URL` are:

- `GET /health` — process-alive probe. Cheap, no DB hit. Open by design (k8s probes need it).
- `GET /ready` and `GET /readyz` — readiness probe. Hits the DB with a `SELECT 1` and timeout. Open by design (k8s probes need it). Not a data-disclosure risk but consumes one pool worker per probe.

`POST /admin/refresh-schema` is gated by `X-Admin-Token` (control 6). It is unauthenticated only if the operator both leaves `AUTH__ADMIN_TOKEN` set to its empty default *and* exposes `/admin/*` past the gateway — and the empty default returns 401 on every call, so a misconfigured deployment surfaces as visible failure rather than silent open access.

When `AUTH__JWKS_URL` is unset, the dynamic surface (`GET /{schema}/{table}`) is unauthenticated and the named query endpoints (`GET /queries/<path>`) are too. `GET /admin/queries` listing the catalog of query names is gated by `X-Admin-Token` regardless of JWT — the catalog is operator-facing recon surface and is closed by default. (The catalog endpoint moved from `/queries` to `/admin/queries` when the admin sub-app was extracted.)

Mitigation for the probe paths: keep IRIS behind a gateway that restricts them to trusted callers, or accept the risk given your network topology.

### Passthrough over HTTP

When `X-DB-Authorization` is used, the Basic-auth credentials cross the network between caller and IRIS. Run IRIS behind TLS. The Trino variant additionally enforces HTTPS on its *own* Trino connection when passthrough is used (Basic auth to Trino over plain HTTP is rejected by Trino itself), but the caller-to-IRIS hop is your responsibility.

### Verbose error mode leaks more than usernames

When passthrough fails at the DB (wrong password, locked account, quota), the DB's error message is wrapped as `DatabaseError` and surfaced. In `ERROR_DETAIL=terse` (the default), the response body collapses to `"Query failed"` and discloses nothing — the full driver text stays in the log only (username-redacted per control 7). `ERROR_DETAIL=safe` returns a stable `{"code": "db.<class>", "message": ...}` shape so clients can branch on the failure class (auth, reachability, permission, timeout) without seeing topology. `ERROR_DETAIL=verbose` returns the raw text plus `deployment` and `database` context fields, and leaks hostnames, port numbers, database names, role names, and constraint identifiers; only run that in dev / inside the trust perimeter.

### Large result sets

Without `MAX_PAGE_SIZE`, a caller can ask for a full table. For a wide table with millions of rows, that's real pod-memory pressure and a long pool-worker occupation. Always set `MAX_PAGE_SIZE` to a sane ceiling for production.

### Schema drift between DDL cache and actual DB

Between a schema change and the next re-harvest, IRIS's view of "what columns exist" diverges from the database's. The failure modes are both safe-fail: a dropped column becomes "Invalid column" (400) until re-harvest, and a new column is invisible to callers until re-harvest. Neither is a security regression, but it is an availability consideration.

### Arbitrary custom query SQL

`queries/*.yaml` files contain operator-authored SQL that IRIS executes. These are a trust boundary: anyone who can commit to that directory can make IRIS run arbitrary SQL against the configured service account. Treat the directory like production SQL migrations — code-review it, restrict who can merge. The SQL runs with whatever privileges the service account has; if you need something scoped tighter, use passthrough instead.

When `CONFIG__SOURCE=git`, the trust boundary moves: anyone who can merge to the configured branch of the config repo can change validation rules and custom-query SQL. The same review-and-restrict discipline applies, just at a different repository. Pulling the YAMLs externally trades "fork the IRIS image to change a YAML" friction for "manage write access to a separate repo" responsibility — pick the side that matches your org's workflow.

When `CONFIG__SOURCE=db`, the trust boundary is the per-deployment database in the config Postgres. Anyone with `INSERT/UPDATE/DELETE` on `iris_config_validations` and `iris_config_queries` can change behavior. Database-level RBAC is the lever — `GRANT INSERT ON iris_config_queries TO iris_dev_writers` for the trusted committers, and use Postgres's audit logging or pgAudit for trail. Same posture as the git option, different mechanism.

### JWKS endpoint as a request-time hard dependency

When `AUTH__JWKS_URL` is set, every authenticated request validates its JWT against the JWKS the IDP serves. The client (`PyJWKClient`) caches keys internally on cache hit, but a cache miss — first request after startup, or first request after the IDP rotates keys — triggers an HTTP fetch to the JWKS URL. If that fetch fails (IDP outage, DNS flake, network partition), the request fails. There is no in-process fallback or "trust the cached key past TTL" behavior today.

Practical consequences:

- **IDP outage** during normal operation surfaces as HTTP errors on every authed request until the IDP recovers. The cached keys are still valid in process; a second JWKS fetch isn't needed for tokens signed with already-cached keys, so steady-state traffic continues. But cold-start traffic (fresh pods after rollout) hits the JWKS URL on the first authed request and fails if the IDP is down.
- **Key rotation** while the IDP is reachable is invisible to callers — `PyJWKClient` refetches on cache miss and updates. Key rotation while the IDP is *unreachable* causes every authed request to fail until the IDP is back.

Mitigations live outside IRIS today: monitor JWKS-fetch latency and failure rate at the gateway / mesh layer, treat IDP outages as a paging event for the auth service rather than IRIS, and consider running the IDP behind the same SLO as the IRIS instances that depend on it. An in-process stale-while-revalidate cache with configurable TTL is an extension point that hasn't shipped because nobody's hit the failure mode in production yet — file an issue if you do.

## What a reviewer should verify

A pre-deployment checklist:

- [ ] DB service account grants reflect the intended scope (the canonical boundary). Optionally drop an `allowlist.yaml` at the config root as a narrowing override when grants are broader than what this deployment should expose.
- [ ] `MAX_PAGE_SIZE` is set to a finite value.
- [ ] `ERROR_DETAIL` left at the `terse` default, or set to `safe` if clients need to branch on failure class. `verbose` is opt-in for dev / trusted-internal only — leaks DB topology to the caller.
- [ ] TLS between callers and IRIS (terminator in front; IRIS itself listens HTTP).
- [ ] Network policy or ingress rules restrict who can reach IRIS, especially the readiness paths.
- [ ] `AUTH__ADMIN_TOKEN` set to a long random secret if you intend to use `/admin/*`. Unset is fail-closed (401 on every call); leaving it empty is fine if you don't use the admin lane.
- [ ] If JWT auth is in use: `AUTH__JWKS_URL`, `AUTH__AUDIENCE`, `AUTH__ISSUER` set; JWKS URL reachable.
- [ ] DB service account has only the grants it needs — `SELECT` on exposed schemas, plus `information_schema` / `all_tab_columns` for the DDL cache. The test-infra compose file demonstrates a metadata-only-vs-data-grant split (`REFERENCES` without `SELECT` on data tables) you can replicate.
- [ ] `LOG_USER_SECRET` is set, or the loss of username correlation in logs is accepted.
- [ ] Custom queries in `queries/*.yaml` reviewed with the same scrutiny as DB migrations.
- [ ] Pipeline that merges to `queries/*.yaml` and `validation/*.yaml` is restricted to trusted committers — the YAML surface executes arbitrary SQL.
- [ ] Branch protection on `dev` requires CI green (so `pip-audit` actually gates merges) and, if `DEPENDABOT_AUTO_MERGE` is `"true"`, that protection is what holds back auto-merge until checks pass.
