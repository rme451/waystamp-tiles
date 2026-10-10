"""Cut one Valhalla tile build into per-region download packages.

Each region (an English ceremonial county, Wales, Scotland; boundaries from
Geofabrik's index-v1.json, derived from OSM) gets every built level-2 tile
(0.25 degrees square) that touches its boundary grown by --buffer-km. All
packages come from the same build, so a tile shared by two regions is
byte-identical in both and any combination installs into one directory.

Writes to --out:
  <region>.zip  the tiles under their Valhalla paths (2/000/828/713.gph) plus
                manifest.json listing each tile with its size and SHA-256.
                Deterministic: same tiles in, same bytes out.
  index.json    the region list the app reads.

Speed: each tile is compressed once (in parallel) and the zips are written
directly from those compressed bytes, since border tiles appear in several
regions.

Usage: cut_regions.py --tiles <tile dir> --geofabrik-index index-v1.json
         --build 20261010 --osm-timestamp 2026-10-09T20:21:43Z
         --valhalla 3.8.3 --base-url <release download URL> --out <dir>
         [--buffer-km 2] [--regions west-yorkshire,lancashire]
"""
import argparse, hashlib, json, math, struct, sys, time, zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from shapely.geometry import box, shape
from shapely.prepared import prep

LEVEL = 2
TILE_DEG = 0.25
COLUMNS = int(360 / TILE_DEG)
COMPRESS_LEVEL = 6
DOS_TIME, DOS_DATE = 0, (0 << 9) | (1 << 5) | 1  # 1980-01-01 00:00
FILE_ATTR = 0o100644 << 16

ATTRIBUTION = ('Contains OpenStreetMap data © OpenStreetMap contributors, '
               'available under the Open Database License (ODbL). '
               'Boundaries and extracts from Geofabrik.')


def default_regions(features):
    """Every English county Geofabrik lists, then Wales and Scotland."""
    england = sorted(p['id'] for p in features if p.get('parent') == 'england')
    return england + ['wales', 'scotland']


def group_of(props):
    return 'England' if props.get('parent') == 'england' else props['name']


def tile_path(tile_id):
    digits = f'{tile_id:09d}'
    return f'{LEVEL}/{digits[0:3]}/{digits[3:6]}/{digits[6:9]}.gph'


def tile_box(tile_id):
    row, col = divmod(tile_id, COLUMNS)
    west, south = col * TILE_DEG - 180, row * TILE_DEG - 90
    return box(west, south, west + TILE_DEG, south + TILE_DEG)


def built_tiles(tile_dir):
    """tile id -> file path for every level-2 tile in the build."""
    tiles = {}
    for path in (Path(tile_dir) / str(LEVEL)).rglob('*.gph'):
        rel = path.relative_to(tile_dir).with_suffix('')
        tiles[int(''.join(rel.parts[1:]))] = path
    return tiles


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def deflate(data):
    c = zlib.compressobj(COMPRESS_LEVEL, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


class Entry:
    """One zip member, already compressed."""
    def __init__(self, name, data):
        self.name = name.encode()
        self.size = len(data)
        self.crc = zlib.crc32(data)
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.packed = deflate(data)


def compress_tile(args):
    tid, path = args
    return tid, Entry(tile_path(tid), Path(path).read_bytes())


def write_zip(zip_path, entries):
    """A plain deflate zip, byte-for-byte deterministic (fixed times and order)."""
    central = []
    with open(zip_path, 'wb') as f:
        for e in entries:
            offset = f.tell()
            f.write(struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, 0, 8, DOS_TIME, DOS_DATE,
                                e.crc, len(e.packed), e.size, len(e.name), 0))
            f.write(e.name)
            f.write(e.packed)
            central.append(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, (3 << 8) | 20, 20, 0, 8,
                                       DOS_TIME, DOS_DATE, e.crc, len(e.packed), e.size,
                                       len(e.name), 0, 0, 0, 0, FILE_ATTR, offset) + e.name)
        start = f.tell()
        for c in central:
            f.write(c)
        f.write(struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(central), len(central),
                            f.tell() - start, start, 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tiles', required=True)
    ap.add_argument('--geofabrik-index', required=True)
    ap.add_argument('--build', required=True, help='build date, YYYYMMDD')
    ap.add_argument('--osm-timestamp', required=True)
    ap.add_argument('--valhalla', required=True)
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--buffer-km', type=float, default=2.0)
    ap.add_argument('--regions', help='comma-separated Geofabrik ids (default: all)')
    a = ap.parse_args()
    t0 = time.monotonic()

    features = {f['properties']['id']: f for f in json.load(open(a.geofabrik_index))['features']}
    props = [f['properties'] for f in features.values()]
    region_ids = a.regions.split(',') if a.regions else default_regions(props)
    tiles = built_tiles(a.tiles)
    if not tiles:
        sys.exit(f'no level-{LEVEL} tiles under {a.tiles}')
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # Which tiles each region gets.
    selected = {}
    for rid in region_ids:
        feature = features[rid]
        geom = shape(feature['geometry'])
        # Degrees of longitude shrink with latitude: grow by the larger (east-west)
        # figure everywhere, which only ever adds tiles.
        grow = a.buffer_km / (111.32 * math.cos(math.radians(geom.centroid.y)))
        geom = geom.buffer(grow)
        hit = prep(geom)
        ids = sorted(tid for tid in tiles if hit.intersects(tile_box(tid)))
        if not ids:
            sys.exit(f'{rid}: no built tiles inside its boundary')
        selected[rid] = (ids, geom.bounds)
    covered = sorted({t for ids, _ in selected.values() for t in ids})
    t1 = time.monotonic()
    print(f'selected tiles for {len(selected)} regions in {t1 - t0:.0f} s', flush=True)

    # Compress every tile any region uses, once.
    with ProcessPoolExecutor() as pool:
        packed = dict(pool.map(compress_tile, [(t, str(tiles[t])) for t in covered], chunksize=4))
    t2 = time.monotonic()
    print(f'compressed {len(packed)} tiles in {t2 - t1:.0f} s', flush=True)

    regions = []
    for rid, (ids, (w, s, e, n)) in selected.items():
        feature = features[rid]
        manifest = {
            'region': rid, 'name': feature['properties']['name'], 'build': a.build,
            'osmTimestamp': a.osm_timestamp, 'valhalla': a.valhalla, 'level': LEVEL,
            'attribution': ATTRIBUTION,
            'tiles': [{'id': t, 'path': tile_path(t), 'size': packed[t].size,
                       'sha256': packed[t].sha256} for t in ids],
        }
        zip_path = out / f'{rid}.zip'
        write_zip(zip_path, [packed[t] for t in ids] +
                  [Entry('manifest.json', json.dumps(manifest, indent=1, ensure_ascii=False).encode())])
        regions.append({
            'id': rid, 'name': feature['properties']['name'],
            'group': group_of(feature['properties']),
            'file': zip_path.name, 'url': f'{a.base_url.rstrip("/")}/{zip_path.name}',
            'size': zip_path.stat().st_size, 'sha256': sha256_file(zip_path),
            'tileCount': len(ids), 'unpackedSize': sum(packed[t].size for t in ids),
            'bbox': [round(w, 4), round(s, 4), round(e, 4), round(n, 4)],
        })
        print(f'{rid:28} {len(ids):3} tiles {regions[-1]["unpackedSize"] / 1e6:7.1f} MB '
              f'-> {regions[-1]["size"] / 1e6:6.1f} MB zip', flush=True)
    t3 = time.monotonic()
    print(f'wrote {len(regions)} zips in {t3 - t2:.0f} s', flush=True)

    index = {'schema': 1, 'build': a.build, 'osmTimestamp': a.osm_timestamp,
             'valhalla': a.valhalla, 'level': LEVEL, 'bufferKm': a.buffer_km,
             'attribution': ATTRIBUTION, 'regions': regions}
    (out / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False))
    for tid in sorted(set(tiles) - set(covered)):
        b = tile_box(tid).bounds
        print(f'  unused tile {tid}: SW corner {b[1]:.2f},{b[0]:.2f}, '
              f'{tiles[tid].stat().st_size / 1e6:.2f} MB')
    print(f'{len(regions)} regions, {len(covered)} of {len(tiles)} built tiles used '
          f'({len(tiles) - len(covered)} in no region), packages '
          f'{sum(r["size"] for r in regions) / 1e9:.2f} GB, {time.monotonic() - t0:.0f} s in all')


if __name__ == '__main__':
    main()
