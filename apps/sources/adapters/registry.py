"""Registry for resolving source adapters without conditional cascades."""

from __future__ import annotations

from collections.abc import Callable

from apps.sources.adapters.base import SourceAdapter


class AdapterNotFoundError(KeyError):
    """Raised when no adapter is registered for a given platform/type."""


class AdapterRegistry:
    """Thread-safe registry mapping (platform, adapter_type) to adapter factories."""

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str], Callable[[], SourceAdapter]] = {}

    def register(
        self,
        platform: str,
        adapter_type: str = "",
        *,
        factory: Callable[[], SourceAdapter] | None = None,
    ) -> Callable[[type[SourceAdapter]], type[SourceAdapter]] | None:
        """Register an adapter class or factory function."""

        def decorator(cls: type[SourceAdapter]) -> type[SourceAdapter]:
            self._registry[(str(platform), str(adapter_type))] = cls
            return cls

        if factory is not None:
            self._registry[(str(platform), str(adapter_type))] = factory
            return None

        return decorator

    def get(self, platform: str, adapter_type: str = "") -> SourceAdapter:
        """Resolve adapter by (platform, adapter_type), falling back to platform-only."""
        key = (str(platform), str(adapter_type))
        if key in self._registry:
            return self._registry[key]()

        # Fallback to (platform, "") if specific adapter_type is not registered
        fallback_key = (str(platform), "")
        if fallback_key in self._registry:
            return self._registry[fallback_key]()

        # Fallback to ("", adapter_type)
        type_fallback_key = ("", str(adapter_type))
        if type_fallback_key in self._registry:
            return self._registry[type_fallback_key]()

        registered = sorted(str(k) for k in self._registry.keys())
        raise AdapterNotFoundError(
            f"No adapter registered for platform='{platform}', adapter_type='{adapter_type}'. "
            f"Available adapters: {registered}"
        )

    def is_registered(self, platform: str, adapter_type: str = "") -> bool:
        """Check if an adapter can be resolved."""
        try:
            self.get(platform, adapter_type)
            return True
        except AdapterNotFoundError:
            return False

    def list_registered(self) -> list[tuple[str, str]]:
        """List all explicitly registered (platform, adapter_type) pairs."""
        return sorted(self._registry.keys())


# Global singleton registry
adapter_registry = AdapterRegistry()
