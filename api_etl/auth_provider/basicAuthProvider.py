import base64, os

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthProvider, AuthError

def _resolve_env(v: str | None) -> str | None:
    if isinstance(v, str) and v.startswith("env:"):
        return os.environ.get(v.split(":", 1)[1])
    return v

class BasicAuthProvider(AuthProvider):
    """
    Auth provider that adds Basic token authorization header for the request
    """

    def get_auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Basic {BasicAuthProvider._get_token_value()}"}

    @staticmethod
    def _get_token_value():
        username = _resolve_env(ApiEtlConfig.auth_basic_username)
        password = _resolve_env(ApiEtlConfig.auth_basic_password)
        if not username or not password:
            raise AuthError("Basic auth credentials not provided")
        basic_payload = f"{username}:{password}"
        return base64.b64encode(basic_payload.encode("utf-8")).decode("utf-8")
