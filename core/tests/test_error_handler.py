"""Tests for the unified-envelope HTTPException handler. (#245)

Builds a minimal FastAPI app with throwing routes, registers the handler,
and asserts the body shape per ERROR_DETAIL mode. Avoids the
variant-specific lifespan / DB pool entirely — only the handler logic
is under test.
"""

from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core import context
from core.errors import handler as error_handler


@dataclass
class _Settings:
    error_detail: str = "terse"
    deployment_name: str = ""


def _make_app(mode: str, deployment_name: str = "", database: str = "postgresql") -> FastAPI:
    s = _Settings(error_detail=mode, deployment_name=deployment_name)
    context.set_context(context.AppContext(
        fetch_all=None, harvest_ddl=None, paramstyle="pyformat",
        settings=s, database=database,
    ))
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise HTTPException(400, "Invalid column 'frobnicate'")

    error_handler.register(app)
    return app


class TestHttpExceptionHandler:
    def teardown_method(self):
        context._ctx = None

    def test_terse_returns_unified_envelope(self):
        client = TestClient(_make_app("terse", deployment_name="inventory"))
        resp = client.get("/boom")
        assert resp.status_code == 400
        assert resp.json() == {
            "error": {
                "code": "validation.bad_request",
                "message": "Invalid column 'frobnicate'",
            }
        }

    def test_safe_returns_unified_envelope(self):
        """Safe mode also projects into the unified envelope — codes
        are assigned by status, validation messages flow through as
        the operator wrote them (post-#245 unification)."""
        client = TestClient(_make_app("safe", deployment_name="inventory"))
        resp = client.get("/boom")
        assert resp.status_code == 400
        assert resp.json() == {
            "error": {
                "code": "validation.bad_request",
                "message": "Invalid column 'frobnicate'",
            }
        }

    def test_verbose_includes_deployment_and_database(self):
        client = TestClient(_make_app(
            "verbose", deployment_name="inventory", database="postgresql"))
        resp = client.get("/boom")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == {
            "code": "validation.bad_request",
            "message": "Invalid column 'frobnicate'",
        }
        assert body["deployment"] == "inventory"
        assert body["database"] == "postgresql"

    def test_verbose_omits_deployment_when_unset(self):
        client = TestClient(_make_app("verbose", database="mysql"))
        resp = client.get("/boom")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["message"] == "Invalid column 'frobnicate'"
        assert "deployment" not in body
        assert body["database"] == "mysql"

    def test_headers_preserved(self):
        """Custom handler must forward HTTPException.headers (e.g. WWW-Authenticate)."""
        s = _Settings(error_detail="terse")
        context.set_context(context.AppContext(
            fetch_all=None, harvest_ddl=None, paramstyle="pyformat",
            settings=s, database="postgresql",
        ))
        app = FastAPI()

        @app.get("/auth-required")
        def auth_required():
            raise HTTPException(401, "Auth required", headers={"WWW-Authenticate": "Bearer"})

        error_handler.register(app)
        resp = TestClient(app).get("/auth-required")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
        assert resp.json()["error"]["code"] == "auth.unauthorized"

    def test_status_code_preserved(self):
        """Custom handler must not collapse status codes to a default."""
        s = _Settings(error_detail="terse")
        context.set_context(context.AppContext(
            fetch_all=None, harvest_ddl=None, paramstyle="pyformat",
            settings=s, database="postgresql",
        ))
        app = FastAPI()

        @app.get("/notfound")
        def notfound():
            raise HTTPException(404, "Not found")

        error_handler.register(app)
        resp = TestClient(app).get("/notfound")
        assert resp.status_code == 404
        assert resp.json() == {
            "error": {"code": "validation.not_found", "message": "Not found"}
        }

    def test_dict_detail_merges_extras_into_envelope(self):
        """Call sites passing a structured detail (e.g. did_you_mean from
        #255) get those keys merged into the error envelope alongside
        the status-derived code."""
        s = _Settings(error_detail="terse")
        context.set_context(context.AppContext(
            fetch_all=None, harvest_ddl=None, paramstyle="pyformat",
            settings=s, database="postgresql",
        ))
        app = FastAPI()

        @app.get("/typo")
        def typo():
            raise HTTPException(
                404,
                detail={
                    "message": "Table 'produts' not found in schema 'public'",
                    "did_you_mean": "products",
                },
            )

        error_handler.register(app)
        resp = TestClient(app).get("/typo")
        assert resp.status_code == 404
        assert resp.json() == {
            "error": {
                "code": "validation.not_found",
                "message": "Table 'produts' not found in schema 'public'",
                "did_you_mean": "products",
            }
        }
