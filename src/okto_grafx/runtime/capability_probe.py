"""Host attribute inspection for explicitly composed engine collaborators.

This is shape observation, not a grant of storage or transaction authority.
Recovery retains its required member list and fail-closed admission policy.
"""

from __future__ import annotations

from inspect import getattr_static


def port_has_attribute(instance: object, name: str) -> bool:
    """Observe a declared member without evaluating it, then try dynamic wrappers.

    A declared property may perform I/O (notably a WAL's ``damage`` property),
    so it must not be evaluated just to validate shape. Only absent static
    declarations use dynamic lookup. Only AttributeError denotes absence;
    every other failure propagates unchanged. No result is cached.
    """
    try:
        getattr_static(instance, name)
    except AttributeError:
        try:
            getattr(instance, name)
        except AttributeError:
            return False
    return True

__all__ = ["port_has_attribute"]
