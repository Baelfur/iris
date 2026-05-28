"""Shared param-contract model for YAML-declared parameter constraints.

Both YAML loaders — ``loaders/validation.py`` (the dynamic-route view
definitions) and ``loaders/queries.py`` (custom-query definitions) —
need the same shape: a set of required params, a set of optional params,
plus the ``validate`` / ``satisfied_by`` methods that enforce them.

Lifted out of ``loaders/validation.py`` so ``loaders/queries.py`` no
longer has to import ``ViewDef`` across loaders to model its own
``QueryDef`` param contract — the dependency was a leaky abstraction
boundary the audit flagged. (#202b)
"""


class ParamContract:
    """Required + optional param surface declared in a YAML file.

    Fields:
    - ``required`` — set of lowercased column names that callers MUST
      constrain (either via simple ``?col=val`` filter or by an equality-
      proving expression in ``$filter``; see :func:`satisfied_by`).
    - ``optional`` — additional column names callers MAY supply.
    - ``allowed`` — convenience union of the above; what
      :meth:`validate` checks against.
    """

    def __init__(self, required: list[str], optional: list[str]):
        self.required: set[str] = {r.lower() for r in required}
        self.optional: set[str] = {o.lower() for o in optional}
        self.allowed: set[str] = self.required | self.optional

    def validate(self, supplied: dict[str, str]) -> str | None:
        """Reject unknown simple-filter param names.

        Returns an error string ready to surface in a 400 body, or
        ``None`` when every supplied key is in ``allowed``. The
        required-param check is the caller's responsibility — it lives
        in :meth:`satisfied_by` because the dynamic route also has to
        consider equality constraints inside ``$filter``.
        """
        supplied_keys = {k.lower() for k in supplied}
        unknown = supplied_keys - self.allowed
        if unknown:
            return f"Parameter(s) not allowed: {', '.join(sorted(unknown))}"
        return None

    def satisfied_by(
        self,
        simple_filter_keys: set[str],
        filter_constrained: set[str],
    ) -> str | None:
        """Check that every ``required`` param is constrained by something.

        ``simple_filter_keys`` are the keys of the simple ``?col=val``
        filter dict. ``filter_constrained`` is the set of columns a
        strict static analysis of ``$filter`` proved are equality-
        constrained on every matching row (see
        :func:`core.engine.expression.analyze.constrained_columns`).
        Either source counts.

        Returns an error string when at least one required param is
        unconstrained, or ``None`` when every required param is
        constrained.
        """
        satisfied = {k.lower() for k in simple_filter_keys} | {
            c.lower() for c in filter_constrained
        }
        missing = self.required - satisfied
        if missing:
            return f"Required parameter(s) missing: {', '.join(sorted(missing))}"
        return None
