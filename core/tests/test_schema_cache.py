"""Tests for core.schema_cache — DDL cache validation and helpers."""

import pytest

from core.engine.schema_cache import (
    parse_column_list,
    validate_columns,
    validate_table,
)


@pytest.fixture
def seeded_cache():
    import core.engine.schema_cache as sc
    sc._cache.clear()
    sc._cache["myschema"] = {
        "mytable": {"id", "name", "category"},
    }
    yield
    sc._cache.clear()


class TestValidateTable:
    def test_valid(self, seeded_cache):
        assert validate_table("myschema", "mytable") is None

    def test_invalid_schema(self, seeded_cache):
        assert validate_table("nope", "mytable") is not None

    def test_invalid_table(self, seeded_cache):
        assert validate_table("myschema", "nope") is not None

    def test_case_insensitive(self, seeded_cache):
        """URL identifiers are case-insensitive."""
        assert validate_table("MYSCHEMA", "MYTABLE") is None


class TestValidateColumns:
    def test_valid(self, seeded_cache):
        assert validate_columns("myschema", "mytable", ["id", "name"]) is None

    def test_invalid(self, seeded_cache):
        assert validate_columns("myschema", "mytable", ["id", "fake"]) is not None

    def test_case_insensitive(self, seeded_cache):
        assert validate_columns("MYSCHEMA", "MyTable", ["ID", "Name"]) is None

    def test_error_message_lists_bad_columns(self, seeded_cache):
        err = validate_columns("myschema", "mytable", ["id", "fake", "ghost"])
        assert "fake" in err["message"]
        assert "ghost" in err["message"]

    def test_did_you_mean_typo_column(self, seeded_cache):
        """A close typo against a real column surfaces a hint (#255)."""
        err = validate_columns("myschema", "mytable", ["nam"])
        assert err is not None
        assert err["did_you_mean"] == ["name"]

    def test_did_you_mean_omitted_for_unrelated(self, seeded_cache):
        """Unrelated token (no close match) doesn't get a bogus hint."""
        err = validate_columns("myschema", "mytable", ["xyzzy"])
        assert err is not None
        assert "did_you_mean" not in err


class TestValidateTableDidYouMean:
    def test_typo_schema_suggests_real(self, seeded_cache):
        err = validate_table("myschma", "mytable")
        assert err is not None
        assert err["did_you_mean"] == "myschema"

    def test_typo_table_suggests_real(self, seeded_cache):
        err = validate_table("myschema", "mytabel")
        assert err is not None
        assert err["did_you_mean"] == "mytable"

    def test_unrelated_table_omits_hint(self, seeded_cache):
        err = validate_table("myschema", "xyzzy")
        assert err is not None
        assert "did_you_mean" not in err


class TestParseColumnList:
    def test_simple(self):
        assert parse_column_list("id,name,city") == ["id", "name", "city"]

    def test_strips_whitespace(self):
        assert parse_column_list("id, name, city") == ["id", "name", "city"]

    def test_strips_orderby_modifiers(self):
        """For $orderby like 'id ASC, name DESC' — takes first token per part."""
        assert parse_column_list("id ASC, name DESC") == ["id", "name"]

    def test_star_excluded(self):
        assert parse_column_list("*") == []
