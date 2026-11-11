"""Where the redaction key comes from. Never from, or into, the database.

With the key and a database dump, anyone could test guesses ("is alice@example.com in
here?") against the fingerprints, so the key lives with the deployment, not the data:

1. AFR_REDACTION_KEY (environment, .env, or Secret Manager when deployed)
2. otherwise <AFR_HOME>/redaction.key (default ~/.afr), generated on first use with
   owner-only permissions. User-level, so the Claude Code hook, which runs in other
   projects' folders, and the server use the same key and their placeholders match.

Changing the key changes every new fingerprint, so the same secret recorded before and
after gets different placeholders. Nothing breaks; they just stop matching.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from flight_recorder.redact.redactor import Redactor

if TYPE_CHECKING:  # pydantic-settings is slow to import; the hook gets by without it
    from flight_recorder.settings import Settings

KEY_NAME = "redaction.key"
MIN_KEY_CHARS = 32

log = logging.getLogger(__name__)


class RedactionKeyError(ValueError):
    pass


def load_key(settings: Settings | None = None, *, key_file: Path | None = None) -> bytes:
    if settings is None:
        from flight_recorder.settings import Settings

        settings = Settings()
    configured = settings.redaction_key.get_secret_value() if settings.redaction_key else None
    return key_from(configured, key_file or settings.afr_home / KEY_NAME)


def key_from(configured: str | None, key_file: Path) -> bytes:
    """The configured key if there is one, else the key file's (made on first use)."""
    if configured is not None:
        key = configured.strip()
        if len(key) < MIN_KEY_CHARS:
            raise RedactionKeyError(
                f"AFR_REDACTION_KEY must be at least {MIN_KEY_CHARS} characters; "
                'make one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return key.encode("utf-8")
    if not key_file.exists():
        _create_key_file(key_file)
    key = key_file.read_text(encoding="utf-8").strip()
    if len(key) < MIN_KEY_CHARS:
        raise RedactionKeyError(f"{key_file} holds a key shorter than {MIN_KEY_CHARS} characters")
    return key.encode("utf-8")


def _create_key_file(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # owner-only where modes apply
    # O_EXCL: if another process created it first, fail rather than overwrite its key.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(secrets.token_hex(32) + "\n")
    log.warning("Generated a redaction key in %s; keep it out of git and the database", path)


def default_redactor(settings: Settings | None = None) -> Redactor:
    return Redactor(load_key(settings))
