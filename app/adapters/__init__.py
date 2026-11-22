"""Data adapters. The pipeline talks to an abstract Feed; Helius is the default
implementation. Keys come from the environment, never hardcoded."""
from .base import Feed
from .helius import HeliusFeed

def get_feed(settings: dict) -> Feed:
    provider = (settings or {}).get("data_provider", "helius")
    if provider in ("helius", "unset"):
        return HeliusFeed()
    raise ValueError(f"unknown data_provider: {provider}")
