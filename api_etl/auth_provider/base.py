import abc


class AuthProvider(metaclass=abc.ABCMeta):
    """
    Auth provider interface that allow modifying request url, headers and payload to  authorization
    """

    @abc.abstractmethod
    def get_auth_header(self) -> dict[str, str]:
        """
        Returns authorization header(s) provided by this authorization method
        """
        raise NotADirectoryError("get_auth_header() not implemented")


class AuthError(Exception):
    pass
