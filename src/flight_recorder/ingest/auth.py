"""API keys for the ingest API (docs/designs/api-key-auth.md).

The server holds only SHA-256 hashes of the keys, as `name:sha256hex` entries in
AFR_API_KEY_HASHES (Secret Manager when deployed). The name is a label for the owner,
saying which entry to delete when revoking; the server doesn't use it. Keys are 32 random
bytes, not passwords, so a plain SHA-256 is enough: there is nothing to brute-force.

`afr serve` decides once, at startup, whether auth is on (auth_mode), and refuses to start
when it would listen beyond loopback without keys, unless a local container has set the
explicit opt-out, AFR_INSECURE_NO_AUTH. That opt-out is refused on Cloud Run.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

LOOPBACK_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})
_ENTRY = re.compile(r"[A-Za-z0-9_.-]+:[0-9a-fA-F]{64}")


class AuthConfigError(ValueError):
    """The key settings would leave the server open or can't be read: don't start."""


@dataclass(frozen=True)
class ApiKeys:
    digests: tuple[bytes, ...]

    @classmethod
    def parse(cls, text: str) -> ApiKeys:
        """Parse AFR_API_KEY_HASHES. Errors give an entry's position, never its content."""
        value = text.strip()
        if not value:
            raise AuthConfigError(
                "AFR_API_KEY_HASHES is set but empty; set name:sha256hex entries, "
                "or delete the line to run without keys on loopback"
            )
        digests: list[bytes] = []
        for position, raw in enumerate(value.split(","), start=1):
            entry = raw.strip()
            if not _ENTRY.fullmatch(entry):
                raise AuthConfigError(f"AFR_API_KEY_HASHES entry {position} is not name:sha256hex")
            digests.append(bytes.fromhex(entry.rsplit(":", 1)[1]))
        return cls(tuple(digests))

    def match(self, token: str) -> bool:
        """Whether the key's hash is configured. Checks every digest, so the time taken
        doesn't depend on which one matched."""
        digest = hashlib.sha256(token.encode()).digest()
        found = False
        for stored in self.digests:
            found |= hmac.compare_digest(digest, stored)
        return found

    def __len__(self) -> int:
        return len(self.digests)


@dataclass(frozen=True)
class Mode:
    auth: ApiKeys | None
    reason: str  # what `serve` prints in brackets after "auth: on" or "auth: off"


def auth_mode(hashes: str | None, host: str, insecure: bool, on_cloud_run: bool) -> Mode:
    """Whether the server may start, and with auth or without. `hashes` is None only when
    AFR_API_KEY_HASHES is absent: an empty value is an error, never "no keys"."""
    if hashes is not None:
        keys = ApiKeys.parse(hashes)
        return Mode(keys, "1 key" if len(keys) == 1 else f"{len(keys)} keys")
    if host in LOOPBACK_BINDS:
        return Mode(None, "loopback, no keys configured")
    if not insecure:
        raise AuthConfigError(
            f"refusing to listen on {host} without API keys: set AFR_API_KEY_HASHES, "
            "or AFR_INSECURE_NO_AUTH=1 for a local container only"
        )
    if on_cloud_run:
        raise AuthConfigError(
            "AFR_INSECURE_NO_AUTH is refused on Cloud Run (K_SERVICE is set): "
            "set AFR_API_KEY_HASHES"
        )
    return Mode(
        None,
        "AFR_INSECURE_NO_AUTH: unauthenticated on all interfaces; "
        "publish the port on 127.0.0.1 only",
    )
