"""Correct pyrogram dispatch-propagation helpers.

pyrogram's ``Update.stop_propagation()``/``...continue_propagation()`` work by
raising ``pyrogram.StopPropagation`` / ``pyrogram.ContinuePropagation``. Both
are ``Exception`` subclasses, so a naive ``try: stop_propagation() except
Exception: pass`` **silently swallows the exception** and the dispatcher never
acts on it — later groups keep running (double-processing / greeting re-fire)
or the current group's remaining handlers stay visible to the next group.

This module provides ``stop()`` / ``continue_()`` that call the underlying
method but **re-raise the propagation signals** so the dispatcher's own
``except pyrogram.StopPropagation`` / ``except pyrogram.ContinuePropagation``
can still halt or advance the pipeline, while still suppressing any genuinely
unexpected (non-propagation) error defensively.

Usage in any handler:

    from utils.propagation import stop
    ...
    stop(message)            # was: try: message.stop_propagation() except Exception: pass
"""

import pyrogram

# StopAsyncIteration is the common base of StopPropagation + ContinuePropagation.
def _is_propagation(exc: BaseException) -> bool:
    return isinstance(exc, pyrogram.StopPropagation) or isinstance(exc, pyrogram.ContinuePropagation)


def stop(update) -> None:
    """Halt dispatch for this update (raise StopPropagation)."""
    try:
        update.stop_propagation()
    except Exception as e:  # noqa: BLE001 - defensive; re-raise the real signal
        if _is_propagation(e):
            raise
        # any other error is unexpected; swallow rather than crash the handler


def continue_(update) -> None:
    """Advance to the next handler in this group (raise ContinuePropagation)."""
    try:
        update.continue_propagation()
    except Exception as e:  # noqa: BLE001 - defensive; re-raise the real signal
        if _is_propagation(e):
            raise
        # swallow unexpected non-propagation errors