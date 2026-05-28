"""Shared integration-test suite — one source, run by each variant.

Each variant's ``tests/test_integration.py`` imports the test functions from
this module (``from core.testing.integration_suite import *``) so pytest
collects them in that variant's test run. Per-variant context — URL paths,
passthrough credentials, the HTTP client wired to the variant's ``app.main``
— comes from fixtures defined in each variant's ``tests/conftest.py``.

**Fixtures this module expects:**

- ``client`` — async httpx client with LifespanManager around the variant's app
- ``schema_path`` — URL prefix for the seeded schema (e.g. ``/public`` or ``/public_user``)
- ``products_path`` — full URL for the ``products`` table (``{schema_path}/products``)
- ``passthrough_creds_good`` — ``(user, password)`` tuple or None if passthrough is unsupported
- ``passthrough_creds_bad`` — ``(user, password)`` tuple or None if passthrough is unsupported

Tests that depend on passthrough skip automatically when the fixture is None.
Variant-specific tests (Trino type coercion, Oracle case quirks, etc.) stay
in the variant's ``test_integration.py`` alongside the ``import *``.
"""

import base64

import pytest


@pytest.fixture(autouse=True)
def verbose_error_responses_for_assertions(client):
    """Tests below assert against specific error-message substrings ("Invalid
    column", "Required parameter(s)", etc.). Production default is terse
    (driver error text in response bodies leaks topology). Flip back
    to verbose for the duration of each integration test so the assertions
    keep telling the reader which path they're exercising. ``client``
    dependency makes sure the lifespan handler has set up the context
    before we touch settings.
    """
    from core.config.settings import ErrorDetail
    from core.context import get_context

    settings = get_context().settings
    original = settings.error_detail
    settings.error_detail = ErrorDetail.VERBOSE
    yield
    settings.error_detail = original


@pytest.fixture(autouse=True)
def empty_view_defs_by_default(client):
    """Most tests in this suite assume no view_defs are loaded — they hit
    the unrestricted route path. The shipped variants include a sample
    ``validation/public/products.yaml`` that requires ``id``; with that
    YAML loaded, every test that uses ``$filter`` alone (without supplying
    simple ``id=1``) hits the required-param check and 400s.

    This fixture clears ``view_defs._defs`` AFTER the lifespan handler
    has run (via ``client`` dep). Tests that want a populated view_def
    explicitly opt back in via the ``view_def_required_id`` fixture.

    No restore — the next test re-runs lifespan via LifespanManager,
    which re-populates ``_defs`` from the YAML. The clear is per-test.

    """
    from core.loaders import validation as view_defs

    view_defs._defs = {}
    yield


@pytest.fixture
def view_def_required_id(empty_view_defs_by_default, schema_path):
    """Explicit opt-in for the ``test_yaml_*`` enforcement tests below.
    Sets a single view def matching the shipped sample ``products.yaml``
    (``required=['id']``, ``optional=['category', 'name']``) under the
    variant's schema path.
    """
    from core.loaders import validation as view_defs
    from core.loaders.validation import ViewDef

    schema = schema_path.strip("/")
    vdef = ViewDef(required=["id"], optional=["category", "name"])
    view_defs._defs.setdefault(schema, {})["products"] = vdef
    yield vdef


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_includes_deployment_when_set(client, monkeypatch):
    """DEPLOYMENT_NAME → /health body includes the deployment field."""
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_name", "inventory")
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "deployment": "inventory"}


@pytest.mark.asyncio
async def test_x_app_deployment_header_when_set(client, monkeypatch):
    """DEPLOYMENT_NAME → every response carries X-{App}-Deployment."""
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_name", "inventory")
    resp = await client.get("/health")
    # Default APP_NAME="app" → header is "X-App-Deployment". Branded
    # deployments set APP_NAME and get e.g. "X-Resource-Direct-Deployment".
    assert resp.headers.get("X-App-Deployment") == "inventory"


@pytest.mark.asyncio
async def test_x_app_deployment_header_omitted_when_unset(client):
    """Unset → no header (silence is fine; matches log/JSON behavior)."""
    from app.config import settings

    assert settings.deployment_name == "", "test fixture left deployment_name set"
    resp = await client.get("/health")
    assert "X-App-Deployment" not in resp.headers


@pytest.mark.asyncio
async def test_openapi_title_is_app_name_when_deployment_unset(client):
    """Default app title is the title-cased APP_NAME when
    DEPLOYMENT_NAME is unset — variant integration tests run with the
    neutral default APP_NAME="app", so the title is "App". The
    title-suffix path is unit-tested in test_app_meta.py — proving it
    via integration would need a fresh app per test (FastAPI title is
    fixed at construction)."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "App"


@pytest.mark.asyncio
async def test_metrics_endpoint_off_by_default(client):
    """ENABLE_METRICS unset → /metrics route doesn't exist. Default-off so
    operators who don't run a Prometheus stack don't get a surprise route
    (and don't need the optional dep installed)."""
    resp = await client.get("/metrics")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tracing_off_by_default_does_not_break_app(client, products_path):
    """OTEL_EXPORTER_OTLP_ENDPOINT unset → no instrumentor attached, app
    works normally. Sanity check that the default-off path doesn't
    introduce surprises (extra middleware, broken routes, etc.)."""
    resp = await client.get(products_path, params={"$filter": "id eq 1"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_basic_filter(client, products_path):
    resp = await client.get(products_path, params={"$filter": "id eq 1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "products"
    assert len(data["elements"]) == 1
    assert data["elements"][0]["id"] == 1


@pytest.mark.asyncio
async def test_select_columns(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "id,name",
            "$filter": "id eq 1",
        },
    )
    assert resp.status_code == 200
    row = resp.json()["elements"][0]
    assert set(row.keys()) == {"id", "name"}


@pytest.mark.asyncio
async def test_orderby(client, products_path):
    """``$orderby`` actually sorts the result. Without an explicit order,
    pagination boundaries are arbitrary; this test verifies the keyword is
    being emitted into the SQL and applied by the DB.
    """
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id",
        },
    )
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["elements"]]
    assert ids == sorted(ids), f"$orderby=id did not sort: {ids}"
    assert len(ids) >= 2, "need >=2 rows for ordering to be meaningful"


@pytest.mark.asyncio
async def test_pagination(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id",
            "$count": "2",
            "$filter": "id gt 0",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["elements"]) == 2
    assert "links" in data
    assert data["links"][0]["rel"] == "next"


@pytest.mark.asyncio
async def test_pagination_offset(client, products_path):
    """``$start_index`` actually skips rows. Compared against the same query
    without ``$start_index`` so the test catches a silent no-op (e.g. the
    OFFSET being dropped or applied with wrong precedence).
    """
    base_params = {
        "$select": "id",
        "$orderby": "id",
        "$count": "10",
        "$filter": "id gt 0",
    }
    page1 = await client.get(products_path, params=base_params)
    assert page1.status_code == 200
    page1_ids = [r["id"] for r in page1.json()["elements"]]
    assert len(page1_ids) >= 4, "need >=4 rows for offset=2 to be meaningful"

    page2 = await client.get(products_path, params={**base_params, "$start_index": "2"})
    assert page2.status_code == 200
    page2_ids = [r["id"] for r in page2.json()["elements"]]
    assert page2_ids == page1_ids[2:], (
        f"start_index=2 should skip first 2 rows; got {page2_ids} vs {page1_ids[2:]}"
    )


@pytest.mark.asyncio
async def test_cursor_pagination_walk(client, products_path):
    """A full cursor walk reaches every row exactly once with no overlap.

    previously, the same walk via ``$start_index`` was O(n²); cursor
    pagination is O(log n) per page. This test validates correctness, not
    performance — that page N+1 starts at the row after page N's last,
    and that all rows are visited without duplicates.
    """
    base_params = {
        "$select": "id",
        "$orderby": "id ASC",
        "$count": "2",
        "$filter": "id gt 0",
    }
    # First page — no cursor in, no walk state. Response carries a cursor
    # token because the page is full and $orderby is set.
    page1 = await client.get(products_path, params=base_params)
    assert page1.status_code == 200
    body1 = page1.json()
    assert "cursor" in body1, "full first page with $orderby should mint a cursor"
    page1_ids = [r["id"] for r in body1["elements"]]
    assert len(page1_ids) == 2

    # Second page — supply the cursor from page 1.
    page2 = await client.get(
        products_path,
        params={**base_params, "$cursor": body1["cursor"]},
    )
    assert page2.status_code == 200
    body2 = page2.json()
    page2_ids = [r["id"] for r in body2["elements"]]

    # No overlap, strictly after page 1.
    assert all(i > page1_ids[-1] for i in page2_ids), (
        f"cursor walk should yield rows strictly after page 1's last id "
        f"({page1_ids[-1]}); got {page2_ids}"
    )


@pytest.mark.asyncio
async def test_cursor_rejected_without_orderby(client, products_path):
    """A cursor is bound to its issuing $orderby. Supplying $cursor
    without $orderby is a 400."""
    # Mint a syntactically valid token via a real first request.
    page1 = await client.get(
        products_path,
        params={"$select": "id", "$orderby": "id ASC", "$count": "2"},
    )
    if "cursor" not in page1.json():
        pytest.skip("not enough rows to mint a cursor for this test")
    resp = await client.get(
        products_path,
        params={"$select": "id", "$count": "2", "$cursor": page1.json()["cursor"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cursor_orderby_mismatch_rejected(client, products_path):
    """A cursor minted for ``id ASC`` is rejected when the next request
    uses ``id DESC``."""
    page1 = await client.get(
        products_path,
        params={"$select": "id", "$orderby": "id ASC", "$count": "2"},
    )
    if "cursor" not in page1.json():
        pytest.skip("not enough rows to mint a cursor for this test")
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id DESC",
            "$count": "2",
            "$cursor": page1.json()["cursor"],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cursor_mutually_exclusive_with_start_index(client, products_path):
    """``$cursor`` + ``$start_index`` together is a 400 — the keyset
    walk and offset pagination are different modes."""
    page1 = await client.get(
        products_path,
        params={"$select": "id", "$orderby": "id ASC", "$count": "2"},
    )
    if "cursor" not in page1.json():
        pytest.skip("not enough rows to mint a cursor for this test")
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id ASC",
            "$count": "2",
            "$cursor": page1.json()["cursor"],
            "$start_index": "0",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_filter_in(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$filter": "category in ('electronics','clothing')",
            "$select": "id,category",
            "$orderby": "id",
        },
    )
    assert resp.status_code == 200
    categories = {r["category"] for r in resp.json()["elements"]}
    assert categories == {"electronics", "clothing"}


@pytest.mark.asyncio
async def test_bad_column_rejected(client, products_path):
    """DDL validation catches invalid columns before hitting DB."""
    resp = await client.get(
        products_path,
        params={
            "$select": "fake_col",
            "$filter": "id eq 1",
        },
    )
    assert resp.status_code == 400
    assert "Invalid column" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_bad_table_rejected(client, schema_path):
    """DDL validation catches invalid tables before hitting DB."""
    resp = await client.get(f"{schema_path}/nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_bad_schema(client):
    resp = await client.get("/nope/products")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_filter_returns_all(client, products_path):
    """Calling the table without any ``$filter`` or simple ``?col=val`` filter
    returns every row in the seeded products fixture (>=6 rows). Catches a
    regression where an unfiltered request gets accidentally narrowed.
    """
    resp = await client.get(products_path, params={"$select": "id"})
    assert resp.status_code == 200
    assert len(resp.json()["elements"]) >= 6


@pytest.mark.asyncio
async def test_orderby_with_direction(client, products_path):
    """Per-column ASC/DESC is accepted and applied."""
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id DESC",
            "$filter": "id gt 0",
        },
    )
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["elements"]]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_orderby_invalid_direction_rejected(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "id",
            "$orderby": "id BOGUS",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_orderby_trailing_sql_rejected(client, products_path):
    """Tokens past column + direction must be rejected — protects against
    raw interpolation of post-comma content into ORDER BY."""
    resp = await client.get(
        products_path,
        params={
            "$orderby": "id, name; SELECT pg_sleep(10)",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_select_trailing_sql_rejected(client, products_path):
    """Multi-token entries in $select are not silently dropped — the rebuilt
    SELECT clause uses only validated identifiers."""
    resp = await client.get(
        products_path,
        params={
            "$select": "id, name; DROP TABLE x",
            "$filter": "id eq 1",
        },
    )
    # The garbage token "DROP" doesn't exist as a column, so validation 400s.
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_legacy_filter_syntax_rejected(client, products_path):
    """Legacy SQL-style $filter is rejected by the closed-grammar parser."""
    resp = await client.get(products_path, params={"$filter": "id=1; DROP TABLE products"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_unknown_ident_in_filter_rejected(client, products_path):
    """Identifiers in $filter are validated against the DDL cache."""
    resp = await client.get(products_path, params={"$filter": "bogus_col eq 1"})
    assert resp.status_code == 400
    assert "Invalid column" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_terse_mode_scrubs_validation_detail(client, products_path, monkeypatch):
    """ERROR_DETAIL=terse collapses validation messages to generic strings.
    Same status code, no leaked column / table / schema name.
    """
    from core.context import get_context

    monkeypatch.setattr(get_context().settings, "error_detail", "terse")
    resp = await client.get(products_path, params={"$select": "fake_col", "id": "1"})
    assert resp.status_code == 400
    detail = resp.json()["error"]["message"]
    assert "fake_col" not in detail
    assert detail == "Bad request"


# --- $groupby / $having ---


@pytest.mark.asyncio
async def test_groupby_returns_distinct_groups(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "category",
            "$filter": "id gt 0",
            "$groupby": "category",
            "$orderby": "category",
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["elements"]
    categories = [r["category"] for r in rows]
    assert len(categories) == len(set(categories))


@pytest.mark.asyncio
async def test_having_filters_groups(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "category",
            "$filter": "id gt 0",
            "$groupby": "category",
            "$having": "category eq 'electronics'",
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["elements"]
    assert len(rows) == 1
    assert rows[0]["category"] == "electronics"


@pytest.mark.asyncio
async def test_filter_and_having_combined(client, products_path):
    """$filter applies before GROUP BY, $having after — both with shared binds."""
    resp = await client.get(
        products_path,
        params={
            "$select": "category",
            "$filter": "price gt 0",
            "$groupby": "category",
            "$having": "category ne 'nonexistent_cat'",
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["elements"]) >= 1


@pytest.mark.asyncio
async def test_having_without_groupby_rejected(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "category",
            "$filter": "id gt 0",
            "$having": "category eq 'electronics'",
        },
    )
    assert resp.status_code == 400
    assert "$having requires $groupby" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_groupby_without_select_rejected(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$filter": "id gt 0",
            "$groupby": "category",
        },
    )
    assert resp.status_code == 400
    assert "explicit $select" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_groupby_select_not_subset_rejected(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "category,name",
            "$filter": "id gt 0",
            "$groupby": "category",
        },
    )
    assert resp.status_code == 400
    assert "not in $groupby" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_groupby_invalid_column_rejected(client, products_path):
    resp = await client.get(
        products_path,
        params={
            "$select": "bogus",
            "$filter": "id gt 0",
            "$groupby": "bogus",
        },
    )
    assert resp.status_code == 400
    assert "Invalid column" in resp.json()["error"]["message"]


# --- Admin / safe params / YAML view defs ---


@pytest.mark.asyncio
async def test_admin_refresh_unconfigured_token_rejected(client):
    """ADMIN_TOKEN unset → admin endpoints fail closed (no silent open path).

    Two assertions under one test now a no-creds request hits the
    dispatcher's fail-closed branch ("credentials required"); a request
    that *supplies* a token surfaces the more specific "not configured"
    message so operators see why their attempt failed."""
    from core.context import get_context

    settings = get_context().settings
    assert settings.auth.admin_token == "", "test fixture left admin_token set"

    # No headers → dispatcher fails closed.
    resp = await client.post("/admin/refresh-schema")
    assert resp.status_code == 401
    assert "credentials required" in resp.json()["error"]["message"].lower()

    # Token supplied but service has no configured token → verify_admin_token's
    # explicit "not configured" message.
    resp = await client.post("/admin/refresh-schema", headers={"X-Admin-Token": "anything"})
    assert resp.status_code == 401
    assert "not configured" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_admin_refresh_missing_token_rejected(client, monkeypatch):
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.post("/admin/refresh-schema")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_refresh_wrong_token_rejected(client, monkeypatch):
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.post("/admin/refresh-schema", headers={"X-Admin-Token": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_refresh_correct_token_accepted(client, monkeypatch):
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.post("/admin/refresh-schema", headers={"X-Admin-Token": "test-secret"})
    assert resp.status_code == 200
    assert "Refreshed" in resp.json()["message"]


@pytest.mark.asyncio
async def test_admin_pool_sizing_requires_token(client):
    resp = await client.get("/admin/pool-sizing")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_pool_sizing_with_token(client, monkeypatch):
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.get("/admin/pool-sizing", headers={"X-Admin-Token": "test-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert "db_max_connections" in body
    assert "db_max_connections_source" in body
    assert body["pool"]["max"] >= 1
    assert "expected_replica_peak" in body
    assert "total_at_peak" in body
    assert "recommendations" in body
    # No deployment field when DEPLOYMENT_NAME is unset.
    assert "deployment" not in body


@pytest.mark.asyncio
async def test_admin_reload_config_requires_token(client):
    resp = await client.post("/admin/reload-config")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_reload_config_with_token(client, monkeypatch):
    """With the right token, reload-config returns counts and (when set)
    the deployment field."""
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.post(
        "/admin/reload-config",
        headers={"X-Admin-Token": "test-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Counts are non-negative ints; exact values depend on how the test
    # fixture seeded view_defs / queries.
    assert isinstance(body["view_defs"], int) and body["view_defs"] >= 0
    assert isinstance(body["queries"], int) and body["queries"] >= 0


@pytest.mark.asyncio
async def test_admin_pool_sizing_includes_deployment_when_set(client, monkeypatch):
    """DEPLOYMENT_NAME → /admin/pool-sizing JSON carries the deployment
    field, useful when one runbook scrapes multiple instances."""
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    monkeypatch.setattr(get_context().settings, "deployment_name", "inventory")
    resp = await client.get("/admin/pool-sizing", headers={"X-Admin-Token": "test-secret"})
    assert resp.status_code == 200
    assert resp.json()["deployment"] == "inventory"


@pytest.mark.asyncio
async def test_simple_param_filter(client, products_path):
    """Simple ?column=value params work as safe equality filters."""
    resp = await client.get(products_path, params={"id": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["elements"]) == 1
    assert data["elements"][0]["id"] == 1


@pytest.mark.asyncio
async def test_simple_param_multi(client, products_path):
    """Multiple simple params AND together."""
    resp = await client.get(
        products_path,
        params={
            "id": "1",
            "category": "electronics",
            "$select": "id,name,category",
        },
    )
    assert resp.status_code == 200
    for row in resp.json()["elements"]:
        assert row["category"] == "electronics"


@pytest.mark.asyncio
async def test_yaml_required_param_missing(client, products_path, view_def_required_id):
    """YAML def requires 'id' — request that doesn't constrain id should fail."""
    resp = await client.get(products_path, params={"category": "electronics"})
    assert resp.status_code == 400
    assert "Required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_yaml_required_not_satisfied_by_range_filter(
    client, products_path, view_def_required_id
):
    """Range/exclusion filters (gt, lt, ne) don't constrain to specific values
    so they don't satisfy a required-id check. (strict semantics)"""
    resp = await client.get(products_path, params={"$filter": "id gt 0"})
    assert resp.status_code == 400
    assert "Required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_yaml_required_satisfied_by_filter_eq(client, products_path, view_def_required_id):
    """$filter with `id eq <value>` satisfies the required-id check."""
    resp = await client.get(products_path, params={"$filter": "id eq 1"})
    assert resp.status_code == 200
    assert len(resp.json()["elements"]) == 1


@pytest.mark.asyncio
async def test_yaml_required_satisfied_by_filter_in(client, products_path, view_def_required_id):
    """$filter with `id in (...)` satisfies the required-id check."""
    resp = await client.get(products_path, params={"$filter": "id in (1, 2, 3)"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_yaml_required_satisfied_by_or_when_both_sides_constrain(
    client, products_path, view_def_required_id
):
    """$filter `id eq 1 or id eq 2` constrains id on both sides of the OR,
    so the required-id check is satisfied."""
    resp = await client.get(products_path, params={"$filter": "id eq 1 or id eq 2"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_yaml_required_not_satisfied_by_or_when_one_side_lacks_constraint(
    client, products_path, view_def_required_id
):
    """$filter `id eq 1 or category eq 'electronics'` doesn't satisfy
    required-id — the OR can match on the category branch alone."""
    resp = await client.get(
        products_path,
        params={
            "$filter": "id eq 1 or category eq 'electronics'",
        },
    )
    assert resp.status_code == 400
    assert "Required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_yaml_required_not_satisfied_by_not(client, products_path, view_def_required_id):
    """`not(id eq 1)` doesn't satisfy required-id — negation breaks the
    constraint guarantee."""
    resp = await client.get(products_path, params={"$filter": "not (id eq 1)"})
    assert resp.status_code == 400
    assert "Required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_yaml_unknown_param_rejected(client, products_path, view_def_required_id):
    """YAML def only allows id/category/name — unknown param rejected."""
    resp = await client.get(
        products_path,
        params={
            "id": "1",
            "hacked": "true",
        },
    )
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["error"]["message"]


# --- Custom queries ---


@pytest.mark.asyncio
async def test_list_queries_requires_admin_token(client):
    """Listing the catalog without an admin token is rejected — operator
    enumerate-able recon surface stays closed by default. Endpoint moved
    from `/queries` to `/admin/queries` when the admin sub-app
    was extracted."""
    resp = await client.get("/admin/queries")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_queries_with_admin_token(client, monkeypatch):
    from core.context import get_context

    monkeypatch.setattr(get_context().settings.auth, "admin_token", "test-secret")
    resp = await client.get("/admin/queries", headers={"X-Admin-Token": "test-secret"})
    assert resp.status_code == 200
    paths = resp.json()["queries"]
    assert any("products_by_category" in p for p in paths)


@pytest.mark.asyncio
async def test_custom_query_with_required_param(client):
    resp = await client.get(
        "/queries/reports/products_by_category",
        params={
            "category": "electronics",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "products_by_category"
    assert len(data["elements"]) >= 1
    for row in data["elements"]:
        assert row["category"] == "electronics"


@pytest.mark.asyncio
async def test_custom_query_missing_required(client):
    resp = await client.get("/queries/reports/products_by_category")
    assert resp.status_code == 400
    assert "Required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_custom_query_no_params(client):
    resp = await client.get("/queries/reports/category_summary")
    assert resp.status_code == 200
    assert len(resp.json()["elements"]) >= 1


@pytest.mark.asyncio
async def test_custom_query_not_found(client):
    """Single-segment /queries/<name> must route to the queries router, not be
    shadowed by the inventory route as schema='queries'."""
    resp = await client.get("/queries/nonexistent")
    assert resp.status_code == 404
    assert "Query" in resp.json()["error"]["message"]


# --- Credential passthrough (skipped when variant doesn't support it) ---


@pytest.mark.asyncio
async def test_passthrough_basic_auth(client, products_path, passthrough_creds_good):
    """Request with Basic auth uses caller's credentials via fetch_all_with_creds."""
    if passthrough_creds_good is None:
        pytest.skip("passthrough not configured for this variant")
    user, pw = passthrough_creds_good
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    resp = await client.get(
        products_path, params={"id": "1"}, headers={"X-DB-Authorization": f"Basic {creds}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["elements"]) == 1
    assert data["elements"][0]["id"] == 1


@pytest.mark.asyncio
async def test_passthrough_bad_creds(client, products_path, passthrough_creds_bad):
    """Bad credentials should return an error, not fall back to service account."""
    if passthrough_creds_bad is None:
        pytest.skip("passthrough not configured for this variant")
    user, pw = passthrough_creds_bad
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    resp = await client.get(
        products_path, params={"id": "1"}, headers={"X-DB-Authorization": f"Basic {creds}"}
    )
    assert resp.status_code == 400


# --- Closed-grammar enforcement ---


@pytest.mark.asyncio
async def test_legacy_sql_syntax_rejected(client, products_path):
    """SQL-fragment ``$filter`` syntax (``=``, ``AND``, etc.) is not part of the
    closed grammar. The parser rejects it as an invalid token regardless of
    configuration.
    """
    resp = await client.get(products_path, params={"$filter": "id=1"})
    assert resp.status_code == 400


# --- Readiness ---


@pytest.mark.asyncio
async def test_ready(client):
    """Readiness endpoint returns 200 when the DB is reachable."""
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_ready_includes_deployment_when_set(client, monkeypatch):
    """DEPLOYMENT_NAME → /ready body includes the deployment field."""
    from core.context import get_context

    monkeypatch.setattr(get_context().settings, "deployment_name", "inventory")
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "deployment": "inventory"}


@pytest.mark.asyncio
async def test_readyz(client):
    """Alias /readyz for k8s convention-compat."""
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
