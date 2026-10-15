"""Shared optimistic-concurrency conflicts, safe to map to HTTP 409."""


class ConflictError(ValueError):
    """The referenced revision or source changed; refresh before retrying."""
