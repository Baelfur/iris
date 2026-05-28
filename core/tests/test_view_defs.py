"""Tests for core.view_defs.ViewDef and core.custom_queries.QueryDef."""

from core.loaders.queries import QueryDef
from core.loaders.validation import ViewDef


class TestViewDefValidate:
    """``validate`` only checks unknown params. Required-param check is
    handled separately by ``satisfied_by`` (#187)."""

    def test_valid_required_and_optional(self):
        vdef = ViewDef(required=["id"], optional=["name"])
        assert vdef.validate({"id": "1", "name": "test"}) is None

    def test_unknown_param(self):
        vdef = ViewDef(required=["id"], optional=["name"])
        err = vdef.validate({"id": "1", "hacked": "true"})
        assert err is not None
        assert "not allowed" in err

    def test_no_restrictions_passes(self):
        vdef = ViewDef(required=[], optional=["id", "name"])
        assert vdef.validate({"id": "1"}) is None
        assert vdef.validate({}) is None

    def test_missing_required_does_NOT_fail_validate(self):
        """Required-param enforcement moved to satisfied_by; validate
        returns None even when required is missing — caller must check
        satisfied_by separately."""
        vdef = ViewDef(required=["id"], optional=["name"])
        assert vdef.validate({"name": "test"}) is None


class TestViewDefSatisfiedBy:
    """``satisfied_by`` accepts either simple-filter keys OR columns
    equality-constrained by ``$filter`` (#187)."""

    def test_satisfied_by_simple_filter(self):
        vdef = ViewDef(required=["id"], optional=[])
        assert vdef.satisfied_by({"id"}, set()) is None

    def test_satisfied_by_filter_constrained(self):
        vdef = ViewDef(required=["id"], optional=[])
        assert vdef.satisfied_by(set(), {"id"}) is None

    def test_satisfied_by_either_source(self):
        """One source can satisfy one required, the other can satisfy a
        different required — the union counts."""
        vdef = ViewDef(required=["id", "tenant"], optional=[])
        assert vdef.satisfied_by({"id"}, {"tenant"}) is None

    def test_unsatisfied_returns_required_message(self):
        vdef = ViewDef(required=["id"], optional=[])
        err = vdef.satisfied_by({"name"}, set())
        assert err is not None
        assert "Required" in err
        assert "id" in err

    def test_no_required_always_satisfied(self):
        vdef = ViewDef(required=[], optional=["id"])
        assert vdef.satisfied_by(set(), set()) is None

    def test_partial_satisfaction_still_fails(self):
        vdef = ViewDef(required=["id", "tenant"], optional=[])
        err = vdef.satisfied_by({"id"}, set())
        assert err is not None
        assert "tenant" in err
        assert "id" not in err  # only the missing one is named

    def test_case_insensitive_matching(self):
        vdef = ViewDef(required=["ID"], optional=[])
        assert vdef.satisfied_by({"id"}, set()) is None
        assert vdef.satisfied_by(set(), {"Id"}) is None


class TestQueryDef:
    def test_validates_params(self):
        vdef = ViewDef(required=["category"], optional=["name"])
        qdef = QueryDef(sql="SELECT 1", view_def=vdef, name="test")
        # validate now only checks unknown params (required-check moved to
        # satisfied_by per #187). Both happy paths return None.
        assert qdef.view_def.validate({"category": "electronics"}) is None
        assert qdef.view_def.validate({}) is None
        # Unknown param still rejected.
        assert qdef.view_def.validate({"category": "x", "hacked": "y"}) is not None
        # Required-param check now lives on satisfied_by.
        assert qdef.view_def.satisfied_by(set(), set()) is not None
        assert qdef.view_def.satisfied_by({"category"}, set()) is None
