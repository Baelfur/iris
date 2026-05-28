"""Handler modules — request-lifecycle orchestration extracted from routes.

Routes stay thin (URL → handler glue). Handlers own the phase-by-phase
orchestration: auth, allowlist, validation, SQL compile, execute, shape.
Variants that need to register additional routes (e.g., Trino's
3-segment ``/{catalog}/{schema}/{view_name}``) import the handler
directly from here rather than reaching into ``routes/``.
"""
