"""
Root-cause extraction for Temporal wrapper exceptions.

An exception escaping an activity reaches the workflow as
ActivityError("Activity task failed") whose .cause chain ends in an
ApplicationError carrying the original message and class name. Persisting
str(e) stores the useless wrapper text — these helpers walk to the root
so run-track / status rows show the real error.

Pure functions of the exception object — safe to call inside workflow code.
"""


def root_error(e: BaseException) -> BaseException:
    """Walk the wrapper chain (ActivityError → ApplicationError → ...) to the root cause."""
    seen: set = set()
    while True:
        cause = getattr(e, "cause", None) or e.__cause__
        if cause is None or id(cause) in seen:
            return e
        seen.add(id(cause))
        e = cause


def error_message(e: BaseException, limit: int = 500) -> str:
    """Root-cause message, e.g. 'ValueError: Unsupported language: Node.js'.

    ApplicationError keeps the original exception class name in .type;
    for anything else fall back to the runtime class name.
    """
    root = root_error(e)
    etype = getattr(root, "type", None)
    if not isinstance(etype, str):  # e.g. TimeoutError.type is a TimeoutType enum
        etype = type(root).__name__
    # FailureError.message is the raw message; str() may already prefix the type
    msg = getattr(root, "message", None) or str(root)
    if msg == etype or msg.startswith(f"{etype}:"):
        return msg[:limit]
    return f"{etype}: {msg}"[:limit]
