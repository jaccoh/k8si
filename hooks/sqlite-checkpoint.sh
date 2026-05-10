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

# Only checkpoint databases that are actively in WAL mode (have a -wal file).
# Databases not in WAL mode don't need checkpointing and may fail the PRAGMA
# call if they're locked or not writable (e.g. Thumbs.db, system databases).
ERRFILE=$(mktemp)
find "$DATA_PATH" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) \
    -exec sh -c '
        db="$1"; errfile="$2"
        [ -f "${db}-wal" ] || exit 0
        if sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" > /dev/null 2>&1; then
            echo "checkpointed: $db"
        else
            echo "checkpoint failed: $db" >&2
            echo fail >> "$errfile"
        fi
    ' _ {} "$ERRFILE" \;

if [ -s "$ERRFILE" ]; then
    ERRORS=$(wc -l < "$ERRFILE")
fi
rm -f "$ERRFILE"

exit "$ERRORS"
