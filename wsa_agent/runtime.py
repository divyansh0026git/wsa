"""Serialize turns that use the legacy process-wide tool runtime.

Reentrant for inline subagents. Separate CLI processes remain independent;
threads in one process must never switch roots during another active turn.
"""
from functools import wraps
from threading import RLock


_turn_lock = RLock()


def serialized_turn(func):
    @wraps(func)
    def guarded(*args, **kwargs):
        with _turn_lock:
            return func(*args, **kwargs)
    return guarded
