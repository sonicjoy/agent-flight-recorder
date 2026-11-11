"""Redaction: secrets and personal details never reach the run store (docs/redaction.md)."""

from flight_recorder.redact.key import RedactionKeyError, default_redactor, key_from, load_key
from flight_recorder.redact.patterns import PATTERNS, Pattern
from flight_recorder.redact.redactor import Redaction, Redactor

__all__ = [
    "PATTERNS",
    "Pattern",
    "Redaction",
    "RedactionKeyError",
    "Redactor",
    "default_redactor",
    "key_from",
    "load_key",
]
