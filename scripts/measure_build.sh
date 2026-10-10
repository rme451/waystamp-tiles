#!/usr/bin/env bash
# Builds Valhalla tiles for one Geofabrik extract and records what the build
# costs: peak memory (anon and total, from the container's cgroup), disk use,
# wall time per stage, and tile count/size per hierarchy level.
#
# Usage: measure_build.sh <geofabrik path> <hierarchy true|false> <concurrency> <out dir>
#   e.g. measure_build.sh europe/united-kingdom/england/west-yorkshire false 4 out
set -euo pipefail

EXTRACT="$1"
HIERARCHY="$2"
CONCURRENCY="$3"
OUT="$(realpath -m "$4")"
IMAGE="${VALHALLA_IMAGE:-ghcr.io/valhalla/valhalla:3.8.3}"
WORK="${WORK_DIR:-/mnt/work}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT"
sudo mkdir -p "$WORK"
sudo chown "$(id -u):$(id -g)" "$WORK"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/build.log"; }

log "extract=$EXTRACT hierarchy=$HIERARCHY concurrency=$CONCURRENCY image=$IMAGE work=$WORK"
free -m | tee -a "$OUT/build.log"
df -h / "$WORK" | tee -a "$OUT/build.log"

t0=$(date +%s)
curl -fsSL -o "$WORK/region.osm.pbf" "https://download.geofabrik.de/${EXTRACT}-latest.osm.pbf"
log "downloaded $(du -m "$WORK/region.osm.pbf" | cut -f1) MB in $(( $(date +%s) - t0 )) s"

jq --argjson h "$HIERARCHY" --argjson c "$CONCURRENCY" '
  .mjolnir.tile_dir = "/data/valhalla_tiles"
  | .mjolnir.tile_extract = "/data/tiles.tar"
  | .mjolnir.admin = "/data/valhalla_tiles/admins.sqlite"
  | .mjolnir.timezone = "/data/valhalla_tiles/timezones.sqlite"
  | .mjolnir.hierarchy = $h
  | .mjolnir.shortcuts = $h
  | .mjolnir.concurrency = $c
' "$SCRIPT_DIR/valhalla.json" > "$WORK/valhalla.json"
cp "$WORK/valhalla.json" "$OUT/valhalla.json"

docker pull -q "$IMAGE"

# The memory cap sits under the runner's 16 GB, so an oversized build is
# OOM-killed inside its container (exit 137) and this script still reports.
t1=$(date +%s)
CID=$(docker run -d --memory=14g --memory-swap=14g \
  --user "$(id -u):$(id -g)" -v "$WORK:/data" --entrypoint /bin/bash "$IMAGE" \
  -c 'valhalla_build_tiles -c /data/valhalla.json /data/region.osm.pbf')
CG="/sys/fs/cgroup/system.slice/docker-${CID}.scope"
log "container $CID, cgroup $CG"

echo "elapsed_s,anon_mib,file_mib,current_mib,work_disk_mib,host_avail_mib" > "$OUT/memory.csv"
while [ "$(docker inspect -f '{{.State.Running}}' "$CID")" = "true" ]; do
  if [ -r "$CG/memory.stat" ]; then
    anon=$(awk '$1=="anon"{print int($2/1048576)}' "$CG/memory.stat")
    file=$(awk '$1=="file"{print int($2/1048576)}' "$CG/memory.stat")
    cur=$(( $(cat "$CG/memory.current") / 1048576 ))
    peak=$(( $(cat "$CG/memory.peak" 2>/dev/null || echo 0) / 1048576 ))
    disk=$(du -sm "$WORK" 2>/dev/null | cut -f1)
    avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
    echo "$(( $(date +%s) - t1 )),$anon,$file,$cur,$disk,$avail" >> "$OUT/memory.csv"
    echo "$peak" > "$OUT/memory_peak_mib.txt"
  fi
  sleep 2
done

EXIT=$(docker inspect -f '{{.State.ExitCode}}' "$CID")
OOM=$(docker inspect -f '{{.State.OOMKilled}}' "$CID")
docker logs "$CID" > "$OUT/valhalla_build.log" 2>&1 || true
docker rm "$CID" > /dev/null
WALL=$(( $(date +%s) - t1 ))
log "build exit=$EXIT oom_killed=$OOM wall=${WALL}s"

# Per-level tile counts and sizes (level 0 = 4°, 1 = 1°, 2 = 0.25° tiles).
{
  echo "level,tiles,total_mib,largest_mib"
  for level in 0 1 2 3; do
    d="$WORK/valhalla_tiles/$level"
    [ -d "$d" ] || continue
    n=$(find "$d" -name '*.gph' | wc -l)
    total=$(du -sm "$d" | cut -f1)
    largest=$(find "$d" -name '*.gph' -printf '%s\n' | sort -n | tail -1 | awk '{printf "%.1f", $1/1048576}')
    echo "$level,$n,$total,$largest"
  done
} > "$OUT/tiles_by_level.csv"

PEAK_ANON=$(cut -d, -f2 "$OUT/memory.csv" | tail -n +2 | sort -n | tail -1)
PEAK_DISK=$(cut -d, -f5 "$OUT/memory.csv" | tail -n +2 | sort -n | tail -1)
{
  echo "## $EXTRACT (hierarchy=$HIERARCHY, concurrency=$CONCURRENCY)"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| Exit code | $EXIT (OOM-killed: $OOM) |"
  echo "| Build wall time | ${WALL} s |"
  echo "| Peak anon memory | ${PEAK_ANON:-?} MiB |"
  echo "| Peak cgroup memory (incl. file cache) | $(cat "$OUT/memory_peak_mib.txt" 2>/dev/null || echo ?) MiB |"
  echo "| Peak work-dir disk | ${PEAK_DISK:-?} MiB |"
  echo
  echo '```'
  cat "$OUT/tiles_by_level.csv"
  echo '```'
} > "$OUT/summary.md"
cat "$OUT/summary.md"

if [ "${KEEP_TILES:-false}" = "true" ] && [ -d "$WORK/valhalla_tiles" ]; then
  tar -C "$WORK" -czf "$OUT/valhalla_tiles.tar.gz" valhalla_tiles
fi

[ "$EXIT" = "0" ]
