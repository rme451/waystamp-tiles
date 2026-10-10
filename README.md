# waystamp-tiles

Build scripts for the offline map-matching ("Snap to path") tiles used by the
Waystamp walking app: [Valhalla](https://github.com/valhalla/valhalla) routing
tiles built from OpenStreetMap extracts.

- **Build region tiles** (`.github/workflows/release.yml`, run by hand): one
  Valhalla 3.8.3 build of Great Britain (level-2 tiles only, no hierarchy),
  cut by `scripts/cut_regions.py` into one zip per English ceremonial county,
  Wales and Scotland. Each zip holds every 0.25° tile touching the region's
  boundary (grown by 2 km) plus a `manifest.json`; a tile shared by two
  regions is identical in both. With `publish` on, the zips and `index.json`
  go out as release `tiles-<YYYYMMDD>`, marked latest, so the region list is
  always at
  `https://github.com/rme451/waystamp-tiles/releases/latest/download/index.json`.
- **Measure a tile build** (`.github/workflows/measure.yml`): builds one
  extract and reports peak memory, disk, time and tile sizes.

## Data licence

The tiles are derived from OpenStreetMap data, © OpenStreetMap contributors,
available under the [Open Database License](https://www.openstreetmap.org/copyright).
Extracts are downloaded from [Geofabrik](https://download.geofabrik.de/).
