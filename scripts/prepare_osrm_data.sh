#!/usr/bin/env bash
# Prepare OSRM routing data for the self-hosted `osrm` compose service.
#
# RUN THIS ON A WORKSTATION, NEVER ON THE PI. osrm-extract/partition/customize
# need several GB of RAM; the serving container only mmaps the result.
# OSRM's data fingerprint checks OS, endianness and pointer size — NOT CPU
# architecture — so files prepared in a linux/amd64 or linux/arm64 container
# on a dev machine load fine on the Pi 5 (both are little-endian 64-bit
# Linux). The fingerprint DOES embed the OSRM version: OSRM_IMAGE below MUST
# stay identical to the image tag in docker-compose.yml's `osrm` service.
#
# Pipeline: geofabrik Uzbekistan PBF (~122 MB) -> osrm-extract (car profile)
# -> osrm-partition -> osrm-customize  == the MLD pipeline required by
# `osrm-routed --algorithm mld`.
#
# Output: ./osrm_data/uzbekistan-latest.osrm.* (~1-1.5 GB). Ship to the Pi
# with rsync (see docs/routing_engine_deploy_rollback.md).
set -euo pipefail

# NOTE THE `-debian` SUFFIX. Upstream stopped publishing bare `vX.Y.Z` image
# tags: `ghcr.io/project-osrm/osrm-backend:v26.8.0` returns MANIFEST_UNKNOWN.
# Only `-debian` (multi-arch: linux/amd64 + linux/arm64), `-amd64-debian` and
# `-arm64-debian` exist, and the Alpine variants were dropped in v26.7.0. A
# naive version bump that omits the suffix fails at pull time.
OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:v26.8.0-debian"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${repo_root}/osrm_data"
PBF="uzbekistan-latest.osm.pbf"
PBF_URL="https://download.geofabrik.de/asia/uzbekistan-latest.osm.pbf"

mkdir -p "${DATA_DIR}"

if [ ! -f "${DATA_DIR}/${PBF}" ]; then
    echo "Downloading ${PBF_URL} ..."
    curl -fL -o "${DATA_DIR}/${PBF}" "${PBF_URL}"
fi

echo "osrm-extract (car profile) ..."
docker run --rm -v "${DATA_DIR}:/data" "${OSRM_IMAGE}" \
    osrm-extract -p /opt/car.lua "/data/${PBF}"

echo "osrm-partition ..."
docker run --rm -v "${DATA_DIR}:/data" "${OSRM_IMAGE}" \
    osrm-partition "/data/uzbekistan-latest.osrm"

echo "osrm-customize ..."
docker run --rm -v "${DATA_DIR}:/data" "${OSRM_IMAGE}" \
    osrm-customize "/data/uzbekistan-latest.osrm"

echo "Done. Prepared files:"
ls -lh "${DATA_DIR}" | grep -v "${PBF}"
