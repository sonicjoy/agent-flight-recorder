"""Where the redaction key comes from. Never from, or into, the database.

With the key and a database dump, anyone could test guesses ("is alice@example.com in
here?") against the fingerprints, so the key lives with the deployment, not the data:

1. AFR_REDACTION_KEY (environment, .env, or Secret Manager when deployed)
2. otherwise .afr/redaction.key, generated on first use with owner-only permissions

Changing the key changes every new fingerprint, so the same secret recorded before and
after gets different placeholders. Nothing breaks; they just stop matching.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from flight_recorder.redact.redactor import Redactor
from flight_recorder.settings import Settings

KEY_FILE = Path(".afr/redaction.key")
MIN_KEY_CHARS = 32

log = logging.getLogger(__name__)


class RedactionKeyError(ValueError):
    pass


def load_key(settings: Settings | None = None, *, key_file: Path = KEY_FILE) -> bytes:
    settings = settings or Settings()
    if settings.redaction_key is not None:
        key = settings.redaction_key.get_secret_value().strip()
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
    path.parent.mkdir(parents=True, exist_ok=True)
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
