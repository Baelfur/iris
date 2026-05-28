"""Promotion scenarios — parametrized over every variant in lib.VARIANTS.

Adding a variant: append to ``VARIANTS`` in ``lib.py``. Pytest discovers
it automatically; no per-variant test file needed. (#119, #108)
"""

from __future__ import annotations

import pytest

from .lib import VARIANTS, Variant, assert_contract, running_container


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda v: v.name)
def test_plain_rollover(
    variant: Variant,
    prev_images: dict[str, str],
    current_images: dict[str, str],
) -> None:
    """Same env + config across PREV and CURRENT. Both halves must pass
    the same contract checks. Catches the regression class "PR broke
    upgrade-in-place."

    Contract checks live in ``lib.assert_contract`` and are stable-
    subset assertions: adding new fields in CURRENT doesn't fail; only
    changing or removing a contract field does.
    """
    prev_tag = prev_images[variant.name]
    current_tag = current_images[variant.name]

    with running_container(prev_tag, variant.port, variant.env) as prev_url:
        assert_contract(prev_url, variant, label=f"PREV/{variant.name}")

    with running_container(current_tag, variant.port, variant.env) as cur_url:
        assert_contract(cur_url, variant, label=f"CURRENT/{variant.name}")
