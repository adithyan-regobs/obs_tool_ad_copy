"""
Environment Variable Component Interface

Contract for provider-specific environment-variable / secret storage backends
(AWS Secrets Manager, AWS SSM Parameter Store, and future GCP Secret Manager,
HashiCorp Vault, Azure Key Vault, ...).

Components translate PER-KEY operations into the provider's storage model, so
callers never branch on how a provider physically stores values:

- Consolidated providers (Secrets Manager): ONE store per service holding all
  keys as a JSON key/value map. ``identifier`` is the store's name or ARN.
- Per-key providers (SSM Parameter Store): one entry per key. ``identifier``
  is the full path of that key's parameter (``get_map`` treats it as a path
  prefix instead).

Adding a provider: subclass this, implement the four methods, and register
the class in ``EnvironmentVariableHandler._build_map()``. No caller changes.

Error convention: read methods (``get_value`` / ``get_map``) swallow provider
errors and return ``None`` / ``(None, None)`` — a missing store is a normal
state (nothing deployed yet). Write methods raise, so deploy flows can record
the per-item failure.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

from app.core.enum import SecretProviderEnum


class BaseEnvironmentVariableComponent(ABC):
    """Interface for one secret/variable storage provider."""

    provider: SecretProviderEnum

    # Whether this provider stores SECRETs (consolidated Secrets Manager) vs
    # plain config VARIABLEs (per-key SSM). Read when reconciling provider keys
    # into the DB so the synced row gets the right variable_type — keeps that
    # secret/plain distinction inside the plugin rather than branching on
    # provider in callers.
    stores_secrets: bool = True

    @abstractmethod
    async def get_value(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> Optional[str]:
        """Live value of one key, or None when the store/key doesn't exist.

        ``key=None`` returns the whole store's value (consolidated providers
        serialize the key map to JSON; per-key providers return the entry's
        value as-is).
        """

    @abstractmethod
    async def get_map(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """All keys of a store as ``(key_map, resolved_identifier)``.

        ``(None, None)`` when the store doesn't exist yet. For per-key
        providers ``identifier`` is a path prefix and the map holds leaf names.
        """

    @abstractmethod
    async def upsert_key(
        self,
        auth_config: dict,
        identifier: str,
        key: str,
        value: str,
        *,
        drop_key: Optional[str] = None,
        description: Optional[str] = None,
        create_identifier: Optional[str] = None,
    ) -> str:
        """Create-or-update one key; returns the store's cloud identifier.

        ``drop_key`` removes an old key in the same write (rename) where the
        provider supports it. ``create_identifier`` is the name to create the
        store at when it doesn't exist yet (defaults to ``identifier`` — pass
        it when ``identifier`` may be a stale ARN of a deleted store).
        """

    @abstractmethod
    async def delete_key(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> None:
        """Remove one key from the store. Idempotent — absent keys are a no-op."""

    async def list_store_keys(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Optional[Dict[str, str]]:
        """Every key in the store ``identifier`` belongs to, mapped to that
        key's OWN cloud identifier — the provider-neutral primitive for using
        the provider as the source of truth for a resource's key set.

        The value is what a synced ``variable_mst`` row should store as its
        ``variable_cloud_identifier`` so ``get_value`` can later resolve it.

        Default (consolidated model, e.g. Secrets Manager): all keys live in one
        store, so every key maps to the same resolved store identifier. Per-key
        providers (SSM) override this to map each key to its own entry path.

        Returns ``None`` when the store can't be read (missing / transient
        error) — callers must NOT treat that as "no keys" and delete on it —
        versus ``{}`` for a store that exists but is empty (every key really is
        gone from the cloud).
        """
        key_map, resolved = await self.get_map(auth_config, identifier)
        if key_map is None:
            return None
        store_id = resolved or identifier
        return {key: store_id for key in key_map}

    def enumeration_scope(self, identifier: str) -> str:
        """The identifier whose ONE enumeration covers ``identifier``.

        Lets the caller de-duplicate enumeration: many rows of a per-key store
        (SSM) share a parent, so all their identifiers resolve to the same scope
        and the store is listed once, not once per key. Consolidated stores
        (Secrets Manager) already are one store — the identifier is its own scope.
        """
        return identifier

    async def list_store_entries(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Optional[Dict[str, Tuple[str, Optional[str]]]]:
        """Like :meth:`list_store_keys` but ALSO carries each key's current value:
        ``{key: (key_cloud_identifier, value)}``. One enumeration then serves both
        the source-of-truth key set AND value resolution, so callers don't re-read
        the store per key. ``None`` when the store can't be read.
        """
        key_map, resolved = await self.get_map(auth_config, identifier)
        if key_map is None:
            return None
        store_id = resolved or identifier
        return {key: (store_id, val) for key, val in key_map.items()}
