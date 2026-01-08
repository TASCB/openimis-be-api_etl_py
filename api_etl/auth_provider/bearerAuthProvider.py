from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthProvider, AuthError


class BearerAuthProvider(AuthProvider):
    """
    Auth provider that add bearer token authorization header for the request
    """

    def get_auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {BearerAuthProvider._get_token_value()}"}

    @staticmethod
    def _get_token_value():
        if not ApiEtlConfig.auth_bearer_token:
            raise AuthError("Bearer token not provided")
        return ApiEtlConfig.auth_bearer_token
