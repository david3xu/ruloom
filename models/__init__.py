"""Model adapters -- the only code that talks to a model."""

from .base import Completion, ModelError, StructuredModel, Usage

__all__ = ["Completion", "ModelError", "StructuredModel", "Usage"]
