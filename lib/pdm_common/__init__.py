"""Shared PdM-Agent library (CFG-01, CFG-03, CFG-07).

Every service loads the asset profile and events file through this package,
so the batch harness and the streaming services read configuration -- and
later resample, window and score -- with the same code.

Rule: nothing in here names an asset-specific signal. Asset facts come from
profiles/<asset_id>/ (enforced by tests/test_profiles.py).

Modules are imported explicitly (``from pdm_common.profile import ...``);
this file stays free of imports so light tools load only what they use.
"""

__version__ = "0.1.0"
