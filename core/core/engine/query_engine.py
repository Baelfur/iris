"""Translate query parameters into SQL. Dialect-aware."""

from dataclasses import dataclass, field
from urllib.parse import urlencode

from . import schema_cache
from .expression import parse as parse_expression
from .paramstyle import BindAccumulator, Binds

RESERVED_PARAMS = {
    "$select",
    "$filter",
    "$orderby",
    "$count",
    "$start_index",
    "$cursor",
    "$groupby",
    "$having",
    "$format",
    "$displayRESTfulReferences",
}


@dataclass
class QueryParams:
    """Container for query parameters."""

    select: str | None = None
    filter_: str | None = None
    orderby: str | None = None
    count: int | None = None
    start_index: int | None = None
    simple_filters: dict[str, str] = field(default_factory=dict)
    groupby: str | None = None
    having: str | None = None
    # Cursor-pagination keyset values. When present (cursor was supplied
    # on the request and validated), build_query emits a keyset WHERE
    # clause rather than an OFFSET. Tuple is (col_dir_pairs, values)
    # where col_dir_pairs is [(col, "ASC"|"DESC"), ...] mirroring the
    # parsed $orderby and values lines up positionally.
    cursor_keyset: tuple[list[tuple[str, str]], list] | None = None


def build_query(
    schema: str,
    view_name: str,
    params: QueryParams,
    paramstyle: str = "pyformat",
    catalog: str | None = None,
) -> tuple[str, Binds]:
    """Build a SQL query from query parameters.

    Args:
        schema: Unqualified schema name (DDL-cache lookup is case-insensitive).
        view_name: Unqualified table/view name.
        params: Parsed query parameters. ``filter_`` and ``having`` flow through
            the closed-grammar parser in :mod:`core.expression`; every
            identifier is validated against the DDL cache and every literal is
            bound. ``groupby`` is a comma-separated column list (enforcement that
            ``select`` is a subset of ``groupby`` lives in the route layer, since
            it's a user-facing rule rather than a SQL-emission concern).
        paramstyle: One of:
            - "pyformat" → %(name)s, dict binds (PG/MySQL/Maria)
            - "named"    → :name, dict binds (Oracle)
            - "qmark"    → ?, list binds (Trino)
        catalog: When set, emit a 3-segment qualified name (``catalog.schema.table``).
            Only meaningful for the Trino variant — Trino's connector model exposes
            multiple catalogs to a single connection. Other variants either don't
            support multi-catalog querying (Postgres connections are scoped to one
            database) or don't have catalogs in this sense (MySQL/MariaDB schemas
            are databases; Oracle is schema/table). Caller is responsible for
            validating that the catalog matches the connection's configured catalog.

    Returns:
        Tuple of (sql_string, binds). Binds is a dict for pyformat/named, a list for qmark.
    """
    if paramstyle == "named":
        table = f"{schema.upper()}.{view_name.upper()}"
    else:
        table = f"{schema.lower()}.{view_name.lower()}"
    if catalog is not None:
        table = f"{catalog.lower()}.{table}"

    cols = params.select if params.select else "*"
    sql = f"SELECT {cols} FROM {table}"

    valid_cols = schema_cache.get_cache().get(schema.lower(), {}).get(view_name.lower(), set())

    def validate_ident(col: str) -> str | None:
        return None if col in valid_cols else f"Invalid column(s): {col}"

    # One accumulator carries both expression-parser binds (autoname `f0`,
    # `f1`, …) and simple-filter binds (caller-keyed `p_{col}`). Counter
    # only advances on autoname calls, so the namespaces stay disjoint
    # without coordination.
    acc = BindAccumulator(paramstyle)

    where_parts: list[str] = []
    if params.filter_:
        where_parts.append(parse_expression(params.filter_, validate_ident, acc))

    for col, val in params.simple_filters.items():
        placeholder = acc.bind(val, key=f"p_{col}")
        where_parts.append(f"{col} = {placeholder}")

    if params.cursor_keyset is not None:
        # Keyset pagination: emit
        #   (col1 OP val1) OR (col1 = val1 AND col2 OP val2) OR ...
        # where OP is > for ASC and < for DESC. This portable shape works
        # on every supported DB without requiring row-constructor support.
        #
        cols_dirs, values = params.cursor_keyset
        or_terms: list[str] = []
        for i in range(len(cols_dirs)):
            and_terms: list[str] = []
            for j in range(i):
                col_j, _ = cols_dirs[j]
                ph_j = acc.bind(values[j], key=f"c_eq_{j}")
                and_terms.append(f"{col_j} = {ph_j}")
            col_i, dir_i = cols_dirs[i]
            op = "<" if dir_i.upper() == "DESC" else ">"
            ph_i = acc.bind(values[i], key=f"c_cmp_{i}")
            and_terms.append(f"{col_i} {op} {ph_i}")
            or_terms.append("(" + " AND ".join(and_terms) + ")")
        where_parts.append("(" + " OR ".join(or_terms) + ")")

    if where_parts:
        sql += f" WHERE {' AND '.join(where_parts)}"

    if params.groupby:
        groupby_cols = schema_cache.parse_column_list(params.groupby)
        sql += f" GROUP BY {', '.join(groupby_cols)}"

    if params.having:
        sql += f" HAVING {parse_expression(params.having, validate_ident, acc)}"

    if params.orderby:
        sql += f" ORDER BY {params.orderby}"

    if params.start_index is not None or params.count is not None:
        if paramstyle == "named":  # Oracle: OFFSET n ROWS FETCH NEXT m ROWS ONLY
            offset = params.start_index or 0
            sql += f" OFFSET {offset} ROWS"
            if params.count is not None:
                sql += f" FETCH NEXT {params.count} ROWS ONLY"
        elif paramstyle == "qmark":  # Trino: OFFSET before LIMIT
            if params.start_index is not None:
                sql += f" OFFSET {params.start_index}"
            if params.count is not None:
                sql += f" LIMIT {params.count}"
        else:  # PG/MySQL/Maria: LIMIT before OFFSET (MySQL requires this order)
            if params.count is not None:
                sql += f" LIMIT {params.count}"
            if params.start_index is not None:
                sql += f" OFFSET {params.start_index}"

    return sql, acc.binds


def build_links(params: QueryParams, row_count: int, cursor: str | None = None) -> list[dict]:
    """Build pagination links.

    When ``cursor`` is supplied (cursor pagination), the next-page URL
    carries ``$cursor=<token>`` instead of ``$start_index``. Otherwise
    falls back to the offset-pagination shape advancing ``$start_index``
    by the page size.
    """
    if params.count is None or row_count < params.count:
        return []

    link_params = {}
    if params.select:
        link_params["$select"] = params.select
    if params.filter_:
        link_params["$filter"] = params.filter_
    if params.orderby:
        link_params["$orderby"] = params.orderby
    if params.groupby:
        link_params["$groupby"] = params.groupby
    if params.having:
        link_params["$having"] = params.having
    for col, val in params.simple_filters.items():
        link_params[col] = val
    if cursor is not None:
        link_params["$cursor"] = cursor
    else:
        next_start = (params.start_index or 0) + params.count
        link_params["$start_index"] = str(next_start)
    link_params["$count"] = str(params.count)

    return [
        {
            "rel": "next",
            "title": "Next interval",
            "href": f"?{urlencode(link_params)}",
        }
    ]


def extract_simple_filters(query_params: dict) -> dict[str, str]:
    """Extract non-reserved query params as simple column=value filters."""
    return {
        k: v for k, v in query_params.items() if k not in RESERVED_PARAMS and not k.startswith("$")
    }
