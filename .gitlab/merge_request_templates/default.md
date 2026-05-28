## Summary

<!-- What this MR changes and why. Bullet-friendly; reviewer should be able to read this in 30 seconds. -->

## Test plan

<!-- What you ran locally and what CI is expected to gate on. -->

- [ ] `ruff check` + `ruff format --check` clean
- [ ] `mypy` clean
- [ ] `docs/build.py --check` passes (if templates / `_config.yml` touched)
- [ ] Core unit suite passes (with `TEST_KAFKA_BROKERS` + `TEST_CONFIG_DSN` env set if Kafka/config-DB paths exercised)
- [ ] Variant integration suite passes for affected variants

## Notes for reviewers

<!-- Anything that doesn't fit into Summary: design choices considered, things explicitly out of scope, follow-ups filed as separate issues. -->
