"""Common safety errors shared by engine adapters and Project orchestration."""


class ApplySafetyError(RuntimeError):
    """Raised when an apply-related operation cannot be completed safely."""


class UnsupportedEngineOperationError(RuntimeError):
    """Raised when an adapter intentionally does not implement a capability."""
