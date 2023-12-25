"""
Identify high quality region
Output two multi-polygon (geojson) and the filtered version of input file
"""

import sys, os, copy, gzip, gc, argparse, warnings, logging
import numpy as np
import pandas as pd
import sklearn.mixture

import shapely
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union
from scipy.spatial import Delaunay
import geojson

# Add parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hexagon_fn import collapse_to_hex

parser = argparse.ArgumentParser()
parser.add_argument('--input', type=str, help='Tab-delimited file including columns X, Y, and they key specified by --filter_based_on. If --feature is provided, need another columns named "gene"')
parser.add_argument('--output', type=str, default='', help='Output file')
parser.add_argument('--output_boundary', type=str, help='Prefix for output boundary files')

parser.add_argument('--feature', type=str, default='', help='')
parser.add_argument('--filter_based_on', type=str, default="Count", help='')
parser.add_argument('--mu_scale', type=float, default=26.67, help='Coordinate to um translate')
parser.add_argument('--max_npts_to_fit_model', type=float, default=1e6, help='')
parser.add_argument('--min_abs_mol_density_squm_dense', type=float, default=0.5, help='Lowerbound for dense tissue region')
parser.add_argument('--min_abs_mol_density_squm', type=float, default=0.02, help='A safe lowerbound to remove very sparse technical noise before fitting mixture model')
parser.add_argument('--hard_threshold', type=float, default=-1, help='If provided, filter by hard threshold (number of molecules per squared um)')
parser.add_argument('--remove_small_polygons', type=float, default=-1, help='If provided, remove small and isolated polygons (squared um)')
parser.add_argument('--radius', type=float, default=15, help='')
parser.add_argument('--hex_n_move', type=int, default=2, help='')
parser.add_argument('--max_edge', type=float, default=-1, help='')
parser.add_argument('--boundary_only', action='store_true', help='')


args = parser.parse_args()
if not os.path.exists(args.input):
    sys.exit("Cannot find input file")
if not args.boundary_only:
    if len(args.output) == 0:
        sys.exit("Please specify output file")
    if not os.path.exists(os.path.dirname(args.output)):
        sys.exit("Output file directory does not exist")

logging.basicConfig(level= getattr(logging, "INFO", None))

key      = args.filter_based_on
n_move   = args.hex_n_move
radius   = args.radius
hex_diam = radius * np.sqrt(3)
hex_area = radius**2*3*np.sqrt(3)/2
max_edge_len = args.max_edge if args.max_edge > 0 else hex_diam * 2

gene_kept = set()
if os.path.exists(args.feature):
    feature = pd.read_csv(args.feature, sep='\t', header=0)
    gene_kept = set(feature.gene.values)

use_header = ["X","Y",key] + ['gene'] if len(gene_kept) > 0 else []
reader = pd.read_csv(gzip.open(args.input,'rb'), sep='\t', usecols=use_header, chunksize=500000)
brc = pd.DataFrame()
for chunk in reader:
    chunk.X /= args.mu_scale
    chunk.Y /= args.mu_scale
    if len(gene_kept) > 0:
        chunk.drop(index = chunk[~chunk.gene.isin(gene_kept)].index, inplace = True)
    sub = collapse_to_hex(chunk, hex_width = hex_diam, n_move = 2, key = key, )
    brc = pd.concat([brc, sub])

ct = brc.groupby(by = ['ID']).agg({key:sum}).reset_index()
brc = brc[["ID",'x','y']].drop_duplicates(subset='ID').merge(right = ct, on = 'ID')
brc.drop(index = brc.index[brc[key] < hex_area * args.min_abs_mol_density_squm], inplace=True)
N = len(brc)
brc.index = np.arange(N)

logging.info(f"Read data, collapsed to {N} hexagons")

dcut_strict = args.hard_threshold * hex_area

vorg = np.log10(brc[key].values)
v = copy.copy(vorg)
if len(vorg) > args.max_npts_to_fit_model:
    v = np.random.choice(v, int(args.max_npts_to_fit_model), replace=False)
v = v.reshape(-1, 1)
gm = sklearn.mixture.GaussianMixture(n_components=2).fit(v)
lab_keep = np.argmax(gm.means_.squeeze())
m0=(10**gm.means_.squeeze()[lab_keep])/hex_area
m1=(10**gm.means_.squeeze()[1-lab_keep])/hex_area

indx = gm.predict(vorg.reshape(-1, 1)) == lab_keep
kept_min = brc.loc[indx, key].min() / hex_area
kept_med = np.median(brc.loc[indx, key]) / hex_area

logging.info(f"Fit 2 component model. {m0:.3f} v.s. {m1:.3f}, cluster min {kept_min:.3f}, median {kept_med:.3f}")

dcut_lenient = (kept_min*.75 + kept_med*.25)

if args.hard_threshold <= 0:
    dcut_strict = kept_med
    if kept_med < args.min_abs_mol_density_squm_dense:
        logging.info(f"Identified density cutoff is lower than that specified by --min_abs_mol_density_squm_dense, will use {args.min_abs_mol_density_squm_dense} instead")
        dcut_strict = args.min_abs_mol_density_squm_dense

logging.info(f"Strict density cutoff {dcut_strict:.3f}, lenient density cutoff {dcut_lenient:.3f}")

dcut_lenient *= hex_area
dcut_strict *= hex_area

def point_to_multipoly(pts, max_edge_len, buffer = 5, poly_area_cutoff = 0):
    tri = Delaunay(pts)
    n_tri = tri.simplices.shape[0]
    max_edge = [max([ np.sqrt(((pts[x[i], :] - pts[x[(i+2) % 3], :])**2).sum() ) for i in [0,1,2] ]) for x in tri.simplices]
    kept_smpl_coord = []
    for i, simplex in enumerate(tri.simplices):
        if max_edge[i] < max_edge_len:
            kept_smpl_coord.append( [ tuple(pts[simplex[i], :]) for i in [0,1,2]] )
    mrg_poly = [ shapely.buffer(Polygon(x),buffer) for x in kept_smpl_coord ]
    mrg_poly = unary_union(mrg_poly)
    if poly_area_cutoff > 0:
        mrg_poly = unary_union([P for P in mrg_poly.geoms if P.area > poly_area_cutoff ])
    return mrg_poly


kept_indx = brc.index[brc[key].gt(dcut_strict)]
pts = brc.loc[kept_indx, ['x', 'y']].values
mrg_poly = point_to_multipoly(pts, max_edge_len, buffer = 5, poly_area_cutoff = args.remove_small_polygons)

f = args.output_boundary + ".boundary.strict.geojson"
with open(f, 'w') as wf:
    geojson.dump(mrg_poly.__geo_interface__, wf)


kept_indx = brc.index[brc[key].gt(dcut_lenient)]
pts = brc.loc[kept_indx, ['x', 'y']].values
mrg_poly = point_to_multipoly(pts, max_edge_len, buffer = radius, poly_area_cutoff = min(args.remove_small_polygons, hex_area*4))

f = args.output_boundary + ".boundary.lenient.geojson"
with open(f, 'w') as wf:
    geojson.dump(mrg_poly.__geo_interface__, wf)

f = args.output_boundary + ".coordinate_minmax.tsv"
xmin, ymin, xmax, ymax = mrg_poly.bounds
with open(f, 'w') as wf:
    wf.write(f"xmin\t{xmin}\nxmax\t{xmax}\nymin\t{ymin}\nymax\t{ymax}\n")

if args.boundary_only:
    sys.exit(0)

if os.path.exists(args.output):
    warnings.warn("Output file already exists, fill be overwritten")

mrg_poly = shapely.prepared.prep(mrg_poly)
ct = 0
for chunk in pd.read_csv(gzip.open(args.input, 'rb'),\
    sep='\t', header=0, chunksize=500000):
    if len(gene_kept) > 0:
        chunk.drop(index = chunk[~chunk.gene.isin(gene_kept)].index, inplace = True)
        if chunk.shape[0] == 0:
            continue
    points = chunk.loc[:, ['X', 'Y']].values / args.mu_scale
    kept = [mrg_poly.contains(Point(*p)) for p in points]
    chunk = chunk.loc[kept, :]
    if chunk.shape[0] == 0:
        continue
    logging.info(f"Output {chunk.shape[0]} rows ...")
    if ct == 0:
        chunk.to_csv(args.output, sep='\t', index=False, header=True)
    else:
        chunk.to_csv(args.output, mode='a', sep='\t', index=False, header=False)
    ct += 1
