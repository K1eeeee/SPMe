"""Configurable W10 3C PyBaMM aging model; importing this package never runs a solve."""

from .config import RunConfig
from .runner import W10Runner

__all__ = ["RunConfig", "W10Runner"]
