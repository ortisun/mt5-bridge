"""The bridge's own logic: journal, sequencing, limits, and safety switches.

Everything here depends only on ``mt5.client``'s ``Mt5Client`` Protocol and
``mt5.models``, never on ``fake_client`` or ``real_client``. It owns no
terminal-specific behavior; it owns the bridge's guarantees.
"""
