import pytest
from pydantic import SecretStr

from vlytics.api.security import Authenticator


def test_authenticator_rejects_equal_role_secrets() -> None:
    secret = SecretStr("synthetic-shared-secret")

    with pytest.raises(ValueError, match="must be distinct"):
        Authenticator(secret, secret)
