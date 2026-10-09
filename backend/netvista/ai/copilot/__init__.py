"""LLM copilot: answers questions about the live network with tools, proposes (never applies) changes."""

from .agent import Copilot, context_window
from .grounding import check as grounding_check

__all__ = ["Copilot", "context_window", "grounding_check"]
