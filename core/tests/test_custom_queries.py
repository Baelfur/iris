"""Tests for custom-query loading and parameter substitution.

The substitution rewrites operator-authored ``:name`` placeholders in YAML
SQL into the driver's paramstyle. Bind keys mirror the operator's param
name verbatim — no synthesized prefix — so a literal token in the SQL can't
collide with a synthesized placeholder. (#74)

The loader requires SQL to start with SELECT or WITH (CTE) unless the YAML
explicitly opts in via ``allow_writes: true``. (#71)
"""

import textwrap

import core.loaders.queries as cq
from core.loaders.queries import load_queries
from core.routes.queries import _substitute_params


class TestSubstituteParams:
    def test_pyformat_rewrites_to_percent(self):
        sql, binds = _substitute_params(
            "SELECT * FROM t WHERE level = :level", {"level": "info"}, "pyformat",
        )
        assert sql == "SELECT * FROM t WHERE level = %(level)s"
        assert binds == {"level": "info"}

    def test_named_passes_through(self):
        """Oracle :name matches the binds-dict key directly — no rewrite."""
        sql, binds = _substitute_params(
            "SELECT * FROM t WHERE level = :level", {"level": "info"}, "named",
        )
        assert sql == "SELECT * FROM t WHERE level = :level"
        assert binds == {"level": "info"}

    def test_qmark_replaces_with_questionmark(self):
        sql, binds = _substitute_params(
            "SELECT * FROM t WHERE level = :level", {"level": "info"}, "qmark",
        )
        assert sql == "SELECT * FROM t WHERE level = ?"
        assert binds == ["info"]

    def test_qmark_preserves_unknown_placeholders(self):
        """Tokens not in params are left as-is — they're literal text."""
        sql, binds = _substitute_params(
            "WHERE level = :level AND msg LIKE ':p_level not allowed'",
            {"level": "info"}, "qmark",
        )
        # :level becomes ?, :p_level stays literal because p_level isn't a param.
        assert sql == "WHERE level = ? AND msg LIKE ':p_level not allowed'"
        assert binds == ["info"]

    def test_no_synthesized_prefix_collision(self):
        """A `:p_<name>` token in the SQL can't collide with a synthesized
        placeholder because there's no synthesized prefix anymore."""
        sql, binds = _substitute_params(
            "WHERE level = :level AND msg <> ':p_level'",
            {"level": "info"}, "pyformat",
        )
        assert sql == "WHERE level = %(level)s AND msg <> ':p_level'"
        assert binds == {"level": "info"}


def _write_yaml(tmp_path, name, body):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


class TestLoadQueriesReadOnly:
    """Loader rejects non-SELECT/WITH SQL unless allow_writes: true. (#71)"""

    def teardown_method(self):
        cq._queries = {}

    def test_select_loads(self, tmp_path):
        _write_yaml(tmp_path, "ok.yaml", """
            sql: SELECT * FROM t
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_with_cte_loads(self, tmp_path):
        _write_yaml(tmp_path, "cte.yaml", """
            sql: |
              WITH active AS (SELECT id FROM t WHERE flag = 1)
              SELECT * FROM active
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_leading_line_comment_tolerated(self, tmp_path):
        _write_yaml(tmp_path, "commented.yaml", """
            sql: |
              -- description of this query
              SELECT 1
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_leading_block_comment_tolerated(self, tmp_path):
        _write_yaml(tmp_path, "blocked.yaml", """
            sql: |
              /* multi-line
                 header */
              SELECT 1
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_delete_rejected_without_opt_in(self, tmp_path, caplog):
        _write_yaml(tmp_path, "del.yaml", """
            sql: DELETE FROM t WHERE id = :id
            params: {required: [id], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0
        assert "allow_writes: true" in caplog.text

    def test_update_rejected_without_opt_in(self, tmp_path):
        _write_yaml(tmp_path, "upd.yaml", """
            sql: UPDATE t SET x = 1 WHERE id = :id
            params: {required: [id], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0

    def test_drop_rejected_without_opt_in(self, tmp_path):
        _write_yaml(tmp_path, "drop.yaml", """
            sql: DROP TABLE t
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0

    def test_explicit_opt_in_loads_write_query(self, tmp_path):
        _write_yaml(tmp_path, "del.yaml", """
            sql: DELETE FROM t WHERE id = :id
            params: {required: [id], optional: []}
            allow_writes: true
        """)
        assert load_queries(str(tmp_path)) == 1


class TestWritableCTEDetection:
    """Pre-#341 the read-only check was leading-token only, so a query
    like ``WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x``
    passed as if it were read-only. Postgres, Oracle, and Trino all
    support data-modifying CTEs.

    Post-#341 the body is scanned for whole-word data-modification
    keywords (with comments + string literals stripped first so
    they don't false-positive)."""

    def teardown_method(self):
        cq._queries = {}

    def test_writable_cte_delete_rejected(self, tmp_path, caplog):
        _write_yaml(tmp_path, "cte_del.yaml", """
            sql: |
              WITH gone AS (DELETE FROM t WHERE id = :id RETURNING *)
              SELECT * FROM gone
            params: {required: [id], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0
        assert "data-modification keyword" in caplog.text.lower() or "delete" in caplog.text.lower()

    def test_writable_cte_update_rejected(self, tmp_path):
        _write_yaml(tmp_path, "cte_upd.yaml", """
            sql: |
              WITH bumped AS (UPDATE t SET counter = counter + 1 RETURNING *)
              SELECT * FROM bumped
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0

    def test_writable_cte_insert_rejected(self, tmp_path):
        _write_yaml(tmp_path, "cte_ins.yaml", """
            sql: |
              WITH added AS (INSERT INTO t (name) VALUES (:name) RETURNING *)
              SELECT * FROM added
            params: {required: [name], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 0

    def test_writable_cte_loads_with_explicit_opt_in(self, tmp_path):
        """When operators genuinely need a write-CTE pattern, the existing
        allow_writes opt-in still works — the check is bypassed entirely."""
        _write_yaml(tmp_path, "cte_optin.yaml", """
            sql: |
              WITH gone AS (DELETE FROM t WHERE id = :id RETURNING *)
              SELECT * FROM gone
            params: {required: [id], optional: []}
            allow_writes: true
        """)
        assert load_queries(str(tmp_path)) == 1


class TestKeywordScanFalsePositives:
    """The body scan must not flag column / identifier names that happen
    to contain a write-verb substring, nor literals / comments that mention
    one. Stripping comments + string literals before the scan is what makes
    the heuristic usable."""

    def teardown_method(self):
        cq._queries = {}

    def test_column_named_delete_at_loads(self, tmp_path):
        """`delete_at` contains DELETE as a substring but not as a whole
        word — \\b boundary in the keyword regex skips it."""
        _write_yaml(tmp_path, "col.yaml", """
            sql: SELECT id, delete_at, created_by FROM products
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_string_literal_containing_keyword_loads(self, tmp_path):
        """`WHERE action = 'delete'` — the string literal is stripped
        before the body scan."""
        _write_yaml(tmp_path, "literal.yaml", """
            sql: SELECT * FROM audit_log WHERE action = 'delete'
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_line_comment_mentioning_keyword_loads(self, tmp_path):
        """`-- DELETE: handle case` — comment stripped before scan."""
        _write_yaml(tmp_path, "comment.yaml", """
            sql: |
              SELECT id, name FROM products
              -- DELETE: handle the deleted-row edge case in the WHERE
              WHERE active = 1
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1

    def test_block_comment_mentioning_keyword_loads(self, tmp_path):
        _write_yaml(tmp_path, "blockcomment.yaml", """
            sql: |
              /* TODO: when DROP CASCADE lands in v2, refactor this */
              SELECT * FROM t
            params: {required: [], optional: []}
        """)
        assert load_queries(str(tmp_path)) == 1
