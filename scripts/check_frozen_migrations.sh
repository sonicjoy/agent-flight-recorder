#!/usr/bin/env sh
# Fails if this branch edits, renames or deletes a migration that already exists on the
# base branch. Databases that ran a migration never run it again, so editing it would
# leave them on a different schema; change the schema with a new migration instead.
#
#   sh scripts/check_frozen_migrations.sh origin/main
set -eu

base="${1:?usage: check_frozen_migrations.sh <base-ref>}"
versions="src/flight_recorder/store/migrations/versions/"

# Compare with where this branch left the base, so new migrations on the base don't count.
fork=$(git merge-base "$base" HEAD)
changed=$(git diff --name-status "$fork" HEAD -- "$versions" | grep -E '^(M|D|R)' || true)

if [ -n "$changed" ]; then
    echo "These migrations are already on $base and must not change. Add a new migration instead:"
    echo "$changed"
    exit 1
fi
echo "OK: no migration from $base was edited."
