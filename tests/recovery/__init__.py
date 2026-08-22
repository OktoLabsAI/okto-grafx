"""Tests for C6: recovery, the unapplied-work ledger, quarantine and verification.

This directory is a package so that a test module can import the fixtures and helpers that live
in its own conftest without depending on which directory pytest happened to put on the import
path -- the same reason the write-ahead log suite is one.
"""
