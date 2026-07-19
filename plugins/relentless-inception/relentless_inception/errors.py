"""Typed runtime errors exposed consistently through CLI and MCP."""


class RelentlessInceptionError(Exception):
    """Base class for expected, user-actionable runtime failures."""


class ConfigError(RelentlessInceptionError):
    """Configuration is missing, unsafe, or internally inconsistent."""


class ProviderError(RelentlessInceptionError):
    """A provider request failed or returned unusable content."""


class BudgetExceeded(RelentlessInceptionError):
    """A configured call, token, cost, or time budget was exhausted."""


class GateRejected(RelentlessInceptionError):
    """A fail-closed verification gate rejected an artifact."""


class RunAborted(RelentlessInceptionError):
    """The run was stopped by its kill switch or an explicit abort."""
