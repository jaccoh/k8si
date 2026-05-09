#!/bin/sh
# Run WAL checkpoint on all SQLite databases under /data.
# Called as a pre-backup hook so restic snapshots a clean, consistent state.

DATA_PATH="${DATA_PATH:-/data}"
ERRORS=0

checkpoint_one() {
    db="$1"
    if sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" > /dev/null 2>&1; then
        echo "checkpointed: $db"
    else
        echo "checkpoint failed: $db" >&2
        return 1
    fi
}

# find -exec runs in the same process, but we need to track errors.
# Use a temp file as a flag since subshells can't propagate variables.
ERRFILE=$(mktemp)
find "$DATA_PATH" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) \
    -exec sh -c 'sqlite3 "$1" "PRAGMA wal_checkpoint(TRUNCATE);" > /dev/null 2>&1 && echo "checkpointed: $1" || { echo "checkpoint failed: $1" >&2; echo fail >> "$2"; }' _ {} "$ERRFILE" \;

if [ -s "$ERRFILE" ]; then
    ERRORS=$(wc -l < "$ERRFILE")
fi
rm -f "$ERRFILE"

exit "$ERRORS"
