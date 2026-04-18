"""shared.tests — regression tests for The Platform.

The pre-migration test suite (test_arbitrage, test_council, test_indicators,
test_market_gateway, test_validation, test_walk_forward, etc.) existed as
compiled `.pyc` only by the time the Tickles-Co repo was created; the `.py`
sources were lost during the VPS-to-repo reconciliation. Phase 38
(Validation + code-analysis + docs freeze) will re-author the full suite.

For Phase 13 onwards, every new phase **must** add its own tests here so we
never regress to a pyc-only state again.
"""
