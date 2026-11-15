"""Value types: secrets never leak into a default ``repr()``."""

from mt5_bridge.mt5.models import Mt5Credentials


def test_credentials_repr_never_includes_the_password() -> None:
    creds = Mt5Credentials(login=1, password="super-secret", server="Demo")

    text = repr(creds)

    assert "super-secret" not in text
    assert "login=1" in text
    assert "server='Demo'" in text
