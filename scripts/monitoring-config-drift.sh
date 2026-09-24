#!/usr/bin/env bash
#
# Print the compose services running an older copy of a monitoring config than
# the repo has (one per line). Read-only; scripts/deploy.sh recreates them.
#
# Single-file bind mounts pin the file a container started with, and services
# like Prometheus read their config only at startup, so a config that was
# edited or replaced (e.g. by `git pull`) stays invisible until the service is
# recreated. A service is stale when a file it mounts from monitoring/ changed
# after the container last started.

set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd -P)
project=${COMPOSE_PROJECT_NAME:-$(basename "$repo")}

for id in $(docker ps -q --filter "label=com.docker.compose.project=$project"); do
    docker inspect --format \
        '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.StartedAt}}{{range .Mounts}}{{if eq .Type "bind"}}|{{.Source}}{{end}}{{end}}' \
        "$id"
done | while IFS='|' read -r -a fields; do
    started=$(date -d "${fields[1]}" +%s)
    for src in "${fields[@]:2}"; do
        if [[ "$src" == "$repo/monitoring/"* && -f "$src" ]] && (( $(stat -c %Z "$src") > started )); then
            echo "${fields[0]}"
            break
        fi
    done
done | sort -u
