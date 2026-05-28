"""Tests for core.paramstyle.BindAccumulator.

The accumulator is the shared bind-emission helper used by the expression
parser, the simple-filter branch in build_query, and the custom-query
substitution. Both call modes (anonymous autoname + caller-keyed) coexist
on a single instance — the autoname counter only advances on anonymous
calls, so the namespaces stay disjoint without coordination. (#42)
"""


from core.engine.paramstyle import BindAccumulator


class TestPyformat:
    def test_anonymous_autoname(self):
        acc = BindAccumulator("pyformat")
        assert acc.bind(1) == "%(f0)s"
        assert acc.bind("x") == "%(f1)s"
        assert acc.binds == {"f0": 1, "f1": "x"}

    def test_caller_keyed(self):
        acc = BindAccumulator("pyformat")
        assert acc.bind(42, key="p_id") == "%(p_id)s"
        assert acc.binds == {"p_id": 42}

    def test_anonymous_and_keyed_coexist(self):
        """Counter only advances on anonymous; keyed binds don't bump it."""
        acc = BindAccumulator("pyformat")
        assert acc.bind(1) == "%(f0)s"
        assert acc.bind("x", key="p_name") == "%(p_name)s"
        assert acc.bind(2) == "%(f1)s"  # counter unchanged by the keyed call
        assert acc.binds == {"f0": 1, "p_name": "x", "f1": 2}

    def test_custom_prefix(self):
        acc = BindAccumulator("pyformat", prefix="h")
        assert acc.bind(1) == "%(h0)s"
        assert acc.binds == {"h0": 1}


class TestNamed:
    def test_anonymous_autoname(self):
        acc = BindAccumulator("named")
        assert acc.bind(1) == ":f0"
        assert acc.bind("x") == ":f1"
        assert acc.binds == {"f0": 1, "f1": "x"}

    def test_caller_keyed(self):
        acc = BindAccumulator("named")
        assert acc.bind(42, key="p_id") == ":p_id"
        assert acc.binds == {"p_id": 42}


class TestQmark:
    def test_positional_list(self):
        acc = BindAccumulator("qmark")
        assert acc.bind(1) == "?"
        assert acc.bind("x") == "?"
        assert acc.binds == [1, "x"]

    def test_key_ignored(self):
        """qmark is positional — caller-supplied keys are dropped."""
        acc = BindAccumulator("qmark")
        assert acc.bind(1, key="anything") == "?"
        assert acc.bind(2) == "?"
        assert acc.binds == [1, 2]


