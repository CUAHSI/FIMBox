"""A client library for accessing FIMBox HAND-FIM API"""

from .client import AuthenticatedClient, Client

__all__ = (
    "AuthenticatedClient",
    "Client",
)
