"""Order state machine: invalid transitions are rejected (e.g. QUOTE_READY -> PRINTING)."""
ALLOWED = {
    "DRAFT": {"ANALYZING", "FAILED", "CANCELLED"},
    "ANALYZING": {"QUOTING", "FAILED"},
    "QUOTING": {"STOCK_CHECK", "FAILED"},
    "STOCK_CHECK": {"QUOTING", "QUOTE_READY", "FAILED"},
    "QUOTE_READY": {"QUOTING", "PAYMENT_PENDING", "CANCELLED"},
    "PAYMENT_PENDING": {"PAID", "FAILED", "CANCELLED"},
    "PAID": {"RESERVED", "FAILED"},
    "RESERVED": {"SCHEDULED", "FAILED"},
    "SCHEDULED": {"PRINTING", "FAILED"},
    "PRINTING": {"COMPLETED", "FAILED"},
    "COMPLETED": set(), "CANCELLED": set(), "FAILED": set(),
}


class InvalidTransition(Exception):
    pass


def transition(current: str | None, new: str) -> str:
    cur = current or "DRAFT"
    if new == cur:
        return new
    if new == "FAILED" and ALLOWED.get(cur):      # any non-terminal state may fail
        return new
    if new not in ALLOWED.get(cur, set()):
        raise InvalidTransition(f"{cur} -> {new} is not allowed")
    return new
