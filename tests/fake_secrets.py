"""Realistic fake secrets, built at run time.

They're generated rather than written out so the secret scanner (gitleaks) doesn't flag
the test files, and so each test run uses fresh values.
"""

from __future__ import annotations

import random
import string

_rng = random.Random()
ALNUM = string.ascii_letters + string.digits
TOKEN = ALNUM + "_-"
BASE64 = ALNUM + "+/"


def random_text(length: int, alphabet: str = ALNUM) -> str:
    """Random characters, with at least one digit, lower- and upper-case letter."""
    while True:
        text = "".join(_rng.choice(alphabet) for _ in range(length))
        has = (any(c.isdigit() for c in text), any(c.islower() for c in text))
        if all(has) and any(c.isupper() for c in text):
            return text


def private_key() -> str:
    marker = "PRIVATE" + " KEY"
    body = "\n".join(random_text(64, BASE64) for _ in range(4))
    return f"-----BEGIN {marker}-----\n{body}\n-----END {marker}-----"


def secrets_by_kind() -> dict[str, str]:
    """One fake per pattern; the value is exactly what must disappear."""
    return {
        "private_key": private_key(),
        "anthropic_key": "sk-" + "ant-api03-" + random_text(48, TOKEN),
        "openai_key": "sk-" + "proj-" + random_text(48, TOKEN),
        "github_token": "gh" + "p_" + random_text(36),
        "aws_access_key": "AK"
        + "IA"
        + "".join(_rng.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
        "gcp_api_key": "AI" + "za" + random_text(35, TOKEN),
        "gcp_oauth_token": "ya" + "29." + random_text(40, TOKEN),
        "jwt": "eyJ"
        + random_text(20, TOKEN)
        + ".eyJ"
        + random_text(30, TOKEN)
        + "."
        + random_text(30, TOKEN),
        "email": f"{random_text(8).lower()}.smith@example.co.uk",
        "phone": "+44 20 7946 " + "".join(_rng.choice(string.digits) for _ in range(4)),
        "high_entropy": random_text(48, BASE64),
    }


def in_context() -> dict[str, tuple[str, str]]:
    """Patterns that need surrounding text: kind -> (text, the part that must disappear)."""
    aws = random_text(40, BASE64)
    bearer = random_text(32, TOKEN)
    password = "Tr0ub4dor-" + random_text(10)
    assigned = random_text(20)
    return {
        "aws_secret_key": (f"aws_secret_access_key = {aws}", aws),
        "bearer_token": (f"Authorization: Bearer {bearer}", bearer),
        "connection_password": (f"postgresql://app:{password}@db.internal:5432/runs", password),
        "secret_assignment": (f'{{"api_key": "{assigned}"}}', assigned),
    }


def prefixed_assignments() -> dict[str, tuple[str, str]]:
    """secret_assignment with the key name spelled as in .env files and code: a prefix
    (DB_PASSWORD, STRIPE_API_KEY) or camelCase (dbPassword). Label -> (text, secret)."""

    def value() -> str:
        return "Tr0ub4dor-" + random_text(10)

    names = {
        "env prefix": "DB_PASSWORD={}",
        "yaml prefix": "POSTGRES_PASSWORD: {}",
        "jwt secret": "JWT_SECRET={}",
        "api key with prefix": "STRIPE_API_KEY={}",
        "camelCase": "dbPassword={}",
        "camelCase token": 'authToken: "{}"',
        "dotted": "spring.datasource.password={}",
        "kebab": "--db-password={}",
        # near-misses: a flag and its value, other assignment operators, more names
        "flag then a space": "docker login --password {} registry",
        "kebab flag then a space": "tool --db-password {} --verbose",
        "mysql -p": "mysql -uroot -h db -p{} app",
        "Go :=": 'password := "{}"',
        "Ruby and PHP =>": "'password' => '{}'",
        "annotated default": 'password: str = "{}"',
        "acronym prefix": "DBPassword={}",
        "trailing digit": "PASSWORD1={}",
        "short suffix": "DB_PASS={}",
        "passphrase": "SSH_PASSPHRASE={}",
        "private key": "PRIVATE_KEY={}",
        "k8s env, YAML": "- name: DB_PASSWORD\n  value: {}",
        "k8s env, JSON": '{{"name": "DB_PASSWORD", "value": "{}"}}',
    }
    cases: dict[str, tuple[str, str]] = {}
    for label, shape in names.items():
        secret = value()
        cases[label] = (shape.format(secret), secret)
    # punctuation that generated passwords hold: the value doesn't stop at it
    semicolon = "Tr0ub;&4dor{" + random_text(6) + "}x"
    cases["; and braces after ="] = (f"DB_PASSWORD={semicolon}", semicolon)
    comma = "P@ss,word;" + random_text(6)
    cases[", and ; after a colon"] = (f"password: {comma}", comma)
    return cases


def in_dicts() -> dict[str, tuple[dict[str, str], str]]:
    """Secrets under a key that names them, as tool inputs and outputs carry them: the
    value alone has no recognisable shape. Label -> (value, secret)."""
    cases: dict[str, tuple[dict[str, str], str]] = {}
    for key in ("password", "api_key", "token", "DB_PASSWORD", "clientSecret", "auth-token"):
        secret = "Corr3ctHorse" + random_text(8)
        cases[key] = ({"user": "app", key: secret}, secret)
    return cases
