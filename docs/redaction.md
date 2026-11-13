# Redaction

Nothing sensitive is ever stored. Before a run or span is written, every free-text field is scanned and each secret or personal detail is replaced with a placeholder. Code: [`redact/`](../src/flight_recorder/redact/). The write path that applies it: [`store/write.py`](../src/flight_recorder/store/write.py).

```
before: "Authorization: Bearer AbC123dEf456GhI789jKl012, mail alice@example.com"
after:  "Authorization: Bearer [REDACTED:bearer_token:d9a61cf2], mail [REDACTED:email:0d8b3da1]"
```

## Placeholders

`[REDACTED:<pattern>:<fingerprint>]`

- **The pattern** says what was there.
- **The fingerprint** is the first 8 hex characters of an HMAC-SHA256 of the value, under the installation's redaction key. The same value gets the same placeholder throughout a database, so a replay shows a key moving from step 3 to a tool call in step 7. The value can't be recovered from it, and without the key nobody can check a guess against it.
- **Only the matched part is replaced.** A connection string keeps its scheme, user and host, and `password=` keeps its key name, so replays stay readable.
- **Redacting again changes nothing:** existing placeholders are skipped.
- **Counts per pattern,** never values, are stored in `runs.redactions` and `spans.redactions`. A run sent in several batches keeps the largest count per pattern from any one batch.

Nothing here can be reversed, by design. There is no encrypted copy of the originals. If a recorded run is ever re-run, for example to make an eval case, placeholders are filled at that time with values the person supplies, or with fakes of the right shape.

## The key

| Where | When |
|---|---|
| `AFR_REDACTION_KEY` (environment or `.env`, at least 32 characters) | Deployed: from Secret Manager |
| `.afr/redaction.key`, generated on first use, owner-only permissions, git-ignored | Local, when the variable isn't set |

The key never goes in the database. With the key and a database dump, someone could test guesses ("is alice@example.com in here?") against the fingerprints. Changing the key changes fingerprints from then on: the same secret recorded before and after gets different placeholders. Nothing breaks, but they no longer match.

## Patterns

Checked in this order; the first match wins.

| Pattern | Matches | Notes |
|---|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` blocks | A key with no END marker (a truncated prompt) is redacted to the end of the text |
| `anthropic_key` | `sk-ant-...` | |
| `openai_key` | `sk-...`, `sk-proj-...`, `sk-svcacct-...` | After `anthropic_key`, which also starts with `sk-` |
| `github_token` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_`, `github_pat_` | |
| `aws_access_key` | `AKIA...`, `ASIA...` (20 characters) | |
| `aws_secret_key` | 40-character secret after `aws...secret...=` | Needs that context; a bare 40-character string is left to `high_entropy` |
| `gcp_api_key` | `AIza` + 35 characters | |
| `gcp_oauth_token` | `ya29.` tokens | |
| `jwt` | `eyJ...eyJ...signature` | |
| `bearer_token` | After `Bearer`, `Basic` or `Token` | |
| `connection_password` | The password in `scheme://user:password@host` | Runs to the last `@` before a host, so an unencoded `/`, `#`, `?` or `@` in a hand-written password is caught too (up to 128 characters). A port and path (`localhost:4873/@types/node`) is left alone |
| `secret_assignment` | The value after a secret's name, of any length: in quotes, to the closing quote (`"password": "two words"`, at most 256 characters); after `:`, the words to the end of the line (`password: correct horse battery staple`); after `=`, one word (`PGPASSWORD=abc123 psql` keeps `psql`). Also a string under such a key in structured data (`{"password": "..."}`, as tool inputs and outputs carry them), replaced whole. A value that says there is none (`null`, `none`, `true`, `false`, `""`) or names a type in code (`password: str`) is left alone | The key name may have a prefix or be camelCase: `DB_PASSWORD=`, `JWT_SECRET=`, `STRIPE_API_KEY=`, `dbPassword=`, `spring.datasource.password=`, and `PGPASSWORD`. A name that only starts with one (`PASSWORD_FILE`, `secret_name`, `max_tokens`) is not a secret's. Also inside JSON strings (`\"api_key\": \"...\"`), which is how OTel sends messages; the result stays valid JSON |
| `email` | Email addresses, in full | |
| `phone` | `+44 20 7946 0958`, `(555) 123-4567`, with 8–15 digits | Bare digit runs are left alone: they're usually ids or timestamps |
| `high_entropy` | Tokens of 32+ base64 or URL-safe characters with digits, lower- and upper-case letters, and entropy ≥ 4 bits per character | Skips hex (git SHAs, trace ids), UUIDs, lowercase paths and `data:...;base64,` content |

### Left alone on purpose

These are tested as near-misses: git SHAs, UUIDs, OTel trace ids, data-URI images, file paths, `max_tokens: 1000`, timestamps, bare digit runs, model specs (`openai:gpt-5.4-mini`), version numbers, long identifiers without digits, and ordinary prose.

### Known gaps and over-matches

- **Secrets with no recognisable shape** (a short password in plain prose, a custom token format) are missed unless they follow `password=` or a similar key.
- **Names and street addresses** aren't detected. That would need an NLP model such as Presidio, too heavy for now.
- **`git@github.com` in an SSH remote** looks like an email address and is redacted.
- **An `@` in a URL's query** (`https://u:pw@host/?to=a@b.com`) is taken as part of the password, so the host is redacted with it. Safe, but less readable.
- **Over-matching:** rising `high_entropy` counts in `spans.redactions` show it catching things it shouldn't. Tune `_looks_random` in `patterns.py` and add the case as a near-miss test.

## Safety properties, tested

- **After `:`, the rest of the line goes**, so prose such as `Token: paste it here` is redacted too. Safe, but less readable.
- **The canary:** a fake of every pattern is planted in every text field of a run and its spans, including dictionary keys, values under a secret's key name, prefixed key names (`DB_PASSWORD=`), a JSON-string attribute and an exception event. A dump of every table must contain none of them. It runs with fresh random fakes each time (`tests/store/test_write.py`).
- **Speed on hostile input:** every pattern starts only at a token boundary and uses possessive quantifiers, so matching stays linear. Hostile inputs of 200,000 characters each finish in well under a second (`test_matching_stays_fast_on_hostile_input`). One pattern was quadratic before this test caught it.
- **One write path:** nothing in `src` other than `store/write.py` writes to `runs` or `spans` (`tests/test_write_path.py`).
