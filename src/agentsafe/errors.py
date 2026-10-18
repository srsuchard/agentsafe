"""Exceptions whose messages are written for the person running the CLI."""


class AgentSafeError(Exception):
    """Base error. The message explains what happened and what to do next."""


class NotInitializedError(AgentSafeError):
    pass


class InvalidSnapshotIdError(AgentSafeError):
    pass


class SnapshotNotFoundError(AgentSafeError):
    pass


class CorruptSnapshotError(AgentSafeError):
    pass


class UnsafePathError(AgentSafeError):
    pass


class RestoreRefusedError(AgentSafeError):
    """The restore was refused before any project file was changed."""


class RestoreFailedError(AgentSafeError):
    """The restore started changing files and then had to stop."""
