"""Reusable test doubles for provider-neutral runtime tests."""

from .model_provider import FailIfCalledProvider, FakeModelProvider

__all__ = ["FailIfCalledProvider", "FakeModelProvider"]
