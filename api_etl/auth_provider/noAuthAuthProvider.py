from api_etl.auth_provider.base import AuthProvider


class NoAuthProvider(AuthProvider):
    """
    Implementation of AuthProvider Interface that does not provide any authorization
    """

    def get_auth_header(self) -> dict[str, str]:
        return {}
