"""Broker error types, shared by brokers.py and executor.py."""


class BrokerError(Exception):
    """Anything that stops us reading or writing at the broker."""


class AuthError(BrokerError):
    """Login failed, token expired, or the daily login budget is used up."""


class RateLimited(BrokerError):
    """Local or remote rate limit hit."""


class RequestRejected(BrokerError):
    """The broker definitively refused the request (HTTP 4xx other than auth/rate).
    The order was NOT placed, so the caller may retry immediately."""
