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

Usage: cut_regions.py --tiles <tile dir> --geofabrik-index index-v1.json
         --build 20261010 --osm-timestamp 2026-10-09T20:21:43Z
         --valhalla 3.8.3 --base-url <release download URL> --out <dir>
         [--buffer-km 2] [--regions west-yorkshire,lancashire]
"""
import argparse, hashlib, json, math, os, sys, zipfile
from pathlib import Path

from shapely.geometry import box, shape
from shapely.prepared import prep

LEVEL = 2
TILE_DEG = 0.25
COLUMNS = int(360 / TILE_DEG)
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

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


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def write_zip(zip_path, manifest, tiles):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for entry in manifest['tiles']:
            info = zipfile.ZipInfo(entry['path'], ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(tiles[entry['id']], 'rb') as f:
                z.writestr(info, f.read(), compresslevel=9)
        info = zipfile.ZipInfo('manifest.json', ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, json.dumps(manifest, indent=1, ensure_ascii=False))


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

    features = {f['properties']['id']: f for f in json.load(open(a.geofabrik_index))['features']}
    props = [f['properties'] for f in features.values()]
    region_ids = a.regions.split(',') if a.regions else default_regions(props)
    tiles = built_tiles(a.tiles)
    if not tiles:
        sys.exit(f'no level-{LEVEL} tiles under {a.tiles}')
    hashes = {tid: sha256(p) for tid, p in tiles.items()}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    regions = []
    covered = set()
    for rid in region_ids:
        feature = features[rid]
        geom = shape(feature['geometry'])
        # Degrees of longitude shrink with latitude: grow by the larger (east-west)
        # figure everywhere, which only ever adds tiles.
        lat = geom.centroid.y
        geom = geom.buffer(a.buffer_km / (111.32 * math.cos(math.radians(lat))))
        hit = prep(geom)
        ids = sorted(tid for tid in tiles if hit.intersects(tile_box(tid)))
        if not ids:
            sys.exit(f'{rid}: no built tiles inside its boundary')
        covered.update(ids)
        manifest = {
            'region': rid, 'name': feature['properties']['name'], 'build': a.build,
            'osmTimestamp': a.osm_timestamp, 'valhalla': a.valhalla, 'level': LEVEL,
            'attribution': ATTRIBUTION,
            'tiles': [{'id': t, 'path': tile_path(t), 'size': tiles[t].stat().st_size,
                       'sha256': hashes[t]} for t in ids],
        }
        zip_path = out / f'{rid}.zip'
        write_zip(zip_path, manifest, tiles)
        w, s, e, n = geom.bounds
        regions.append({
            'id': rid, 'name': feature['properties']['name'],
            'group': group_of(feature['properties']),
            'file': zip_path.name, 'url': f'{a.base_url.rstrip("/")}/{zip_path.name}',
            'size': zip_path.stat().st_size, 'sha256': sha256(zip_path),
            'tileCount': len(ids), 'unpackedSize': sum(t['size'] for t in manifest['tiles']),
            'bbox': [round(w, 4), round(s, 4), round(e, 4), round(n, 4)],
        })
        print(f'{rid:28} {len(ids):3} tiles {regions[-1]["unpackedSize"] / 1e6:7.1f} MB '
              f'-> {regions[-1]["size"] / 1e6:6.1f} MB zip', flush=True)

    index = {'schema': 1, 'build': a.build, 'osmTimestamp': a.osm_timestamp,
             'valhalla': a.valhalla, 'level': LEVEL, 'bufferKm': a.buffer_km,
             'attribution': ATTRIBUTION, 'regions': regions}
    (out / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False))
    unused = len(tiles) - len(covered)
    for tid in sorted(set(tiles) - covered):
        b = tile_box(tid).bounds
        print(f'  unused tile {tid}: SW corner {b[1]:.2f},{b[0]:.2f}, '
              f'{tiles[tid].stat().st_size / 1e6:.2f} MB')
    print(f'{len(regions)} regions, {len(covered)} of {len(tiles)} built tiles used '
          f'({unused} in no region), packages {sum(r["size"] for r in regions) / 1e9:.2f} GB')


if __name__ == '__main__':
    main()
