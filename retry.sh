
#!/usr/bin/env bash

# Retry a failed command up to 3 times.
# Default delay between attempts: 10 seconds.

retry() {
    local max_attempts="${RETRY_MAX_ATTEMPTS:-3}"
    local delay_seconds="${RETRY_DELAY_SECONDS:-10}"
    local attempt=1
    local status=0

    while true; do
        if "$@"; then
            return 0
        else
            status=$?
        fi

        if (( attempt >= max_attempts )); then
            echo "ERROR: Command failed after ${attempt} attempts (exit code: ${status})." >&2
            printf 'Command: %q' "$@" >&2
            printf '\n' >&2
            return "$status"
        fi

        echo "Attempt ${attempt}/${max_attempts} failed."
        echo "Retrying in ${delay_seconds} seconds..."
        sleep "$delay_seconds"

        attempt=$((attempt + 1))
    done
}
