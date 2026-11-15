"""Terminal contract: the sole boundary through which the bridge speaks to MT5.

``client.py`` defines the ``Mt5Client`` Protocol every caller in the bridge
depends on. ``fake_client.py`` is a deterministic, in-memory implementation
used by every test on macOS/Linux. ``real_client.py`` (Windows-only, added
only once it type-checks cleanly) is the sole module allowed to import the
``MetaTrader5`` package (plan rule 40).
"""
