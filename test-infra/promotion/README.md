# Promotion harness

Boots an image at a previous git ref **and** the current working
tree against the same data DB and same env, then validates both via the
same contract checks. Catches the regression class "PR broke upgrade-
in-place." Pytest-driven; one test per (variant × scenario).

All five variants (postgres / mysql / mariadb / oracle / trino) are wired in.
The harness was established in #119 and extended to the remaining four
in the PR closing #120-#123. New variants land by appending to
`lib.py::VARIANTS`.

## Why pytest, not shell

The first cut of this harness was shell scripts (one driver, one lib,
one scenario per file). It got replaced with pytest because:

- **Type safety.** The shell version had a `((TOTAL++))` bug that
  aborted under `set -e` when the variable started at 0. Python
  doesn't have that footgun.
- **JSON parsing.** Shell did `grep -q '"name":"products"'` against
  raw response bodies. Whitespace variants broke it. Python parses
  the JSON and does typed field access.
- **Cross-variant scaling.** Shell required a per-variant directory
  with copied lib + scenarios. Pytest parametrizes over `VARIANTS`;
  adding a variant is 8 lines of config in `lib.py`.
- **Reuses existing patterns.** `core/tests/test_kafka_logging_
  integration.py` and `test_config_source_db.py::TestRealPostgres`
  already use the skip-when-env-unset + real-service pattern. The
  promotion harness is the same shape with image lifecycle added.

## Usage

```bash
# Prereqs: bring up test-infra DBs for the variants you want to test.
cd test-infra && docker compose up -d postgres                   # postgres only
cd test-infra && docker compose up -d                            # everything

# From the repo root, run the full matrix:
PROMOTION_PREV_REF=v1.0.0 pytest test-infra/promotion/ -v

# Or filter to one variant:
PROMOTION_PREV_REF=v1.0.0 pytest test-infra/promotion/ -v -k postgres
PROMOTION_PREV_REF=$(git rev-parse HEAD~5) pytest test-infra/promotion/ -v -k 'postgres or mysql'
```

Without `PROMOTION_PREV_REF` set the suite skips — same shape as
`TEST_KAFKA_BROKERS` and `TEST_CONFIG_DSN`.

## What's PREV_REF

The git ref the harness should treat as the "previous" version. No
default — operators decide. Three sensible values:

| Choice | When |
|---|---|
| Tagged release (`v1.0.0`) | After the first GA tag. The cleanest story. |
| Specific SHA on `main` | Pre-1.0. Pin to a known-good commit and re-run before each merge. |
| Branch name (`main`) | "Does my PR break upgrade-from-main?" Useful in PR review. |

The harness does `git worktree add` for the PREV_REF in a temp
directory, builds the image from there, removes the worktree on
completion. Your working tree stays untouched.

## What gets checked

`lib.py::assert_contract` — stable-subset assertions across both
halves:

- `/health` returns `{"status": "ok"}`
- `<products_path>?id=1` returns the products envelope with `id == 1`
- An invalid-column probe returns 400, not 500

The `products_path` is per-variant: postgres / mysql / mariadb use
`/public/products`; oracle uses `/public_user/products` (Oracle's "schema"
is the user). Trino sets `products_path=None` because the test-infra
memory catalog starts empty — its contract collapses to `/health` plus
a 404-on-bad-schema probe.

These are **stable subset assertions**, not full-response equality.
Adding a new field in CURRENT (e.g. `deployment` in `/health`) doesn't
fail; only removing or changing a contract field does. That's the
actual forward-compat contract the service makes.

## Scenarios

Just one in this PR — `test_plain_rollover` (same env, both halves,
contract checks). Additional scenarios from #119 are deferred:

- **default-flip** — N+1 changes a default. Needs scenario-specific
  knowledge of which default flipped, so this lives closer to the PR
  that flips one.
- **new-required-env-var** — N-vintage env file fails with a clear
  error. Add when a concrete required-var change happens
  (`CONFIG_SOURCE` in #107 was this kind of change).
- **new-optional-feature-off** — already covered by plain-rollover
  when `PREV_REF` is recent enough.

## Limitations

- Builds two images per session (~30s each warm cache, several minutes
  cold). PREV builds are SHA-tagged so re-runs against the same
  `PREV_REF` skip the rebuild. CURRENT always rebuilds — it's what's
  under test.
- Manual harness; no CI integration. CI integration is deferred until
  a tagged baseline exists. When it lands, it's a single
  `@pytest.mark.promotion` filter + a workflow step.
- Parallel runs collide on host port 18222 (variant-specific port +
  10000). One harness invocation at a time.

## Files

```
test-infra/promotion/
├── README.md             this file
├── __init__.py           package marker (relative imports)
├── conftest.py           session fixtures: prev_ref, prev_images, current_images
├── lib.py                VARIANTS, image build helpers, contract checks
└── test_promotion.py     parametrized scenarios
```
