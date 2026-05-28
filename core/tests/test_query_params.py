"""Tests for the closed-grammar param single-source-of-truth module. (#272)

The drift-prevention contract: the FastAPI dependency
(``ClosedGrammarParams``) and the OpenAPI dict generator
(``openapi_dicts``) derive from one declaration. The tests pin the
projection so a regression that drops a param or a constraint surfaces
loudly.
"""

import inspect

from fastapi.params import Query as QueryParam

from core.engine.query_params import ClosedGrammarParams, openapi_dicts

EXPECTED_PARAMS = {
    "$select": ("select", "string", None),
    "$filter": ("filter_", "string", None),
    "$orderby": ("orderby", "string", None),
    "$count": ("count", "integer", 1),
    "$start_index": ("start_index", "integer", 0),
    "$cursor": ("cursor", "string", None),
    "$groupby": ("groupby", "string", None),
    "$having": ("having", "string", None),
}


class TestClosedGrammarParams:
    def test_dependency_exposes_all_eight_params(self):
        sig = inspect.signature(ClosedGrammarParams.__init__)
        py_names = {n for n in sig.parameters if n != "self"}
        assert py_names == {v[0] for v in EXPECTED_PARAMS.values()}

    def test_dependency_query_aliases(self):
        sig = inspect.signature(ClosedGrammarParams.__init__)
        seen_aliases = set()
        for py_name, p in sig.parameters.items():
            if py_name == "self":
                continue
            assert isinstance(p.default, QueryParam)
            seen_aliases.add(p.default.alias)
        assert seen_aliases == set(EXPECTED_PARAMS.keys())

    def test_integer_params_have_constraints(self):
        from core.engine.query_params import _extract_ge

        sig = inspect.signature(ClosedGrammarParams.__init__)
        assert _extract_ge(sig.parameters["count"].default) == 1
        assert _extract_ge(sig.parameters["start_index"].default) == 0


class TestOpenAPIDicts:
    def test_names_match_dependency(self):
        names = {d["name"] for d in openapi_dicts()}
        assert names == set(EXPECTED_PARAMS.keys())

    def test_types_and_constraints_propagate(self):
        for d in openapi_dicts():
            py_name, expected_type, expected_ge = EXPECTED_PARAMS[d["name"]]
            assert d["schema"]["type"] == expected_type
            if expected_ge is not None:
                assert d["schema"]["minimum"] == expected_ge
            else:
                assert "minimum" not in d["schema"]

    def test_descriptions_present(self):
        for d in openapi_dicts():
            assert d["description"]

    def test_all_optional(self):
        for d in openapi_dicts():
            assert d["required"] is False
            assert d["in"] == "query"
