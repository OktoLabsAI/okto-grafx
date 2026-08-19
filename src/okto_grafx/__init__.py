"""Okto Grafx: an embedded graph database for Python.

Correct under multi-process concurrency, verifiable on disk and recoverable by construction.

This module is the public entry point of the package. The foundation build exposes the version
only; the public facade (connect, Database, Transaction, QueryResult) is added by C11 once the
engine exists. Errors already have their supported import path in :mod:`okto_grafx.errors`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__: str = "0.1.0"
