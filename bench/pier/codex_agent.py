"""Codex adapter with the benchmark's exact outbound network policy."""

from pier.agents.installed.codex import Codex
from pier.models.agent.network import NetworkAllowlist


BENCHMARK_NETWORK_DOMAINS = (
    "api.openai.com",
    "api.x.ai",
    "auth.openai.com",
    "chatgpt.com",
)


class BenchmarkCodex(Codex):
    """Permit only Codex/xAI model and Codex authentication endpoints."""

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=list(BENCHMARK_NETWORK_DOMAINS))
