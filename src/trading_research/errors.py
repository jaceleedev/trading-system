"""Shared input errors without dependencies on dataset or strategy modules."""


class DataError(ValueError):
    """An input cannot be used without silently inventing investment data."""
