"""Cancellation shared by native and visual companion backends."""


class Interrupted(RuntimeError):
    """The human paused or steered the current operation."""
