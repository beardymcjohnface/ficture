import sys, os, copy, gzip, logging
import pickle, argparse
import numpy as np
import pandas as pd

from scipy.sparse import *
import sklearn.neighbors
import sklearn.preprocessing
from sklearn.decomposition import LatentDirichletAllocation as LDA
# Add parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unit_loader import UnitLoader
from online_lda import OnlineLDA
from lda_minibatch import Minibatch

parser = argparse.ArgumentParser()
parser.add_argument('--input', type=str, help='')
parser.add_argument('--output', '--output_pref', type=str, help='')
parser.add_argument('--unit_label', default = 'random_index', type=str, help='Which column to use as unit identifier')
parser.add_argument('--unit_attr', type=str, nargs='+', default=[], help='')
parser.add_argument('--feature', type=str, default='', help='')
parser.add_argument('--feature_label', default = "gene", type=str, help='Which column to use as feature identifier')
parser.add_argument('--key', default = 'count', type=str, help='')
parser.add_argument('--train_on', default = '', type=str, help='')
parser.add_argument('--log', default = '', type=str, help='files to write log to')

parser.add_argument('--nFactor', type=int, default=10, help='')
parser.add_argument('--minibatch_size', type=int, default=512, help='')
parser.add_argument('--min_ct_per_feature', type=int, default=1, help='')
parser.add_argument('--min_ct_per_unit', type=int, default=20, help='')
parser.add_argument('--thread', type=int, default=1, help='')
parser.add_argument('--epoch', type=int, default=1, help='How many times to loop through the full data')
parser.add_argument('--epoch_id_length', type=int, default=-1, help='')
parser.add_argument('--use_model', type=str, default='', help="Use provided model to transform input data")
parser.add_argument('--prior', type=str, default='', help="Dirichlet parameters for the global parameter beta (factor x gene)")
parser.add_argument('--tau', type=int, default=9, help='')
parser.add_argument('--kappa', type=float, default=0.7, help='')
parser.add_argument('--N', type=float, default=1e4, help='')
parser.add_argument('--debug', action='store_true')
parser.add_argument('--verbose', action='store_true')
parser.add_argument('--overwrite', action='store_true')

args = parser.parse_args()
if args.log != '':
    try:
        logging.basicConfig(filename=args.log, filemode='a', encoding='utf-8', level=logging.INFO)
    except:
        logging.basicConfig(level= getattr(logging, "INFO", None))
else:
    logging.basicConfig(level= getattr(logging, "INFO", None))

if args.use_model != '' and not os.path.exists(args.use_model):
    sys.exit("Invalid model file")

unit_attr = [x.lower() for x in args.unit_attr]
key = args.key.lower()
train_on = args.train_on.lower()
unit_key = args.unit_label.lower()
gene_key = args.feature_label.lower()
if train_on == '':
    train_on = key
adt = {unit_key:str, gene_key:str, key:int, train_on:int}
adt.update({x:str for x in unit_attr})
print(unit_attr)

### Basic parameterse
b_size = args.minibatch_size
K = args.nFactor

### Input
# Required columns: unit ID, gene, key
required_header = [unit_key,gene_key,train_on]
if not os.path.exists(args.input):
    sys.exit("ERROR: cannot find input file.")
with gzip.open(args.input, 'rt') as rf:
    header = rf.readline().strip().split('\t')
header = [x.lower() for x in header]
for x in required_header:
    if x not in header:
        sys.exit("Input file must have at least 3 columns: unit label, feature label, count, matching the customized column names (case insensitive) --unit_label, --feature_label, and --key/--train_on")

use_existing_model = False
model_f = args.output+".model.p"
if not os.path.isfile(model_f):
    model_f = args.output + ".model_matrix.tsv.gz"
if os.path.exists(args.use_model):
    model_f = args.use_model
if not args.overwrite and os.path.exists(model_f):
    if model_f.endswith('.tsv.gz') or model_f.endswith('tsv'):
        model_mtx = pd.read_csv(model_f, sep='\t')
        feature_kept=list(model_mtx.gene)
        model_mtx = np.array(model_mtx.iloc[:, 1:])
        M, K = model_mtx.shape
        lda = OnlineLDA(vocab=feature_kept,K=K,N=1e4,thread=args.thread,tol=1e-3)
        lda.init_global_parameter(model_mtx.T)
    else:
        lda = pickle.load( open( model_f, "rb" ) )
        feature_kept = lda.feature_names_in_
        lda.feature_names_in_ = None
        K, M = lda.components_.shape
        lda.n_jobs = args.thread
    use_existing_model = True
    ft_dict = {x:i for i,x in enumerate( feature_kept ) }
    logging.warning(f"Read existing model from\n{model_f}\n use --overwrite to allow the model files to be overwritten\n{M} genes will be used")

factor_header = [str(x) for x in range(K)]
chunksize=100000 if args.debug else 1000000
if not use_existing_model:
    if not os.path.exists(args.feature):
        sys.exit("Unable to read feature list")
    prior = None
    if os.path.isfile(args.prior):
        prior = pd.read_csv(args.prior, sep='\t', header=0, index_col=0)
        if prior.shape[1] != K:
            sys.exit(f"ERROR: number of factors in --prior file does not match --nFactor ({K})")
    ### Use only the provided list of features
    with gzip.open(args.feature, 'rt') as rf:
        fheader = rf.readline().strip().split('\t')
    fheader = [x.lower() for x in fheader]
    feature=pd.read_csv(args.feature, sep='\t', skiprows=1, names=fheader, dtype={gene_key:str, key:int})
    feature = feature[feature[key] >= args.min_ct_per_feature]
    feature.sort_values(by=key,ascending=False,inplace=True)
    feature.drop_duplicates(subset=gene_key,keep='first',inplace=True)
    feature_kept = list(feature[gene_key].values)
    ft_dict = {x:i for i,x in enumerate( feature_kept ) }
    M = len(feature_kept)

    logging.info(f"Start fitting model ... {M} genes will be used")

    if prior is None:
        lda = LDA(n_components=K, learning_method='online', batch_size=b_size, n_jobs = args.thread, learning_offset = args.tau, learning_decay = args.kappa, verbose = 0)
    else:
        prior = prior[prior.index.isin(ft_dict)]
        prior.index = prior.index.map(lambda x: ft_dict[x])
        prior_mtx = np.ones((K, M)) * .5
        prior_mtx[:,prior.index] += prior.values.T
        lda = OnlineLDA(vocab=feature_kept,K=K,N=args.N,tau0=args.tau,kappa=args.kappa,thread=args.thread,tol=1e-3)
        lda.init_global_parameter(prior_mtx)
        mt = prior_mtx.sum(axis =1)
        mt = " ".join([f"{x:.2e}" for x in mt])
        logging.info(f"Read prior for global parameters. Prior magnitude: {mt}")

    epoch = 0
    n_unit = 0
    n_batch = 0
    while epoch < args.epoch:
        reader = pd.read_csv(gzip.open(args.input, 'rt'), \
                sep='\t',chunksize=chunksize, skiprows=1, names=header, \
                usecols=[unit_key,gene_key,train_on], dtype=adt)
        batch_obj =  UnitLoader(reader, ft_dict, train_on, \
            batch_id_prefix=args.epoch_id_length, \
            min_ct_per_unit=args.min_ct_per_unit,
            unit_id=unit_key,unit_attr=[])
        while batch_obj.update_batch(b_size):
            N = batch_obj.mtx.shape[0]
            x1 = np.median(batch_obj.brc[train_on].values)
            x2 = np.mean(batch_obj.brc[train_on].values)
            logging.info(f"Made DGE {N}, median/mean count: {x1:.1f}/{x2:.1f}")
            n_unit += N
            if prior is None:
                _ = lda.partial_fit(batch_obj.mtx)
                if args.verbose or args.debug:
                    logl = lda.score(batch_obj.mtx) / batch_obj.mtx.shape[0]
                    logging.info(f"Epoch {epoch}, finished {n_unit} units. batch logl: {logl:.4f}")
            else:
                st = 0
                while st < N:
                    ed = st + b_size
                    if N - ed < b_size:
                        ed = N
                    logl = lda.update_lambda(Minibatch(batch_obj.mtx[st:ed, :]))
                    st = ed
                    logging.info(f"{n_batch}-th batch logl: {logl:.4f}")
                    n_batch += 1
                logging.info(f"Epoch {epoch}, finished {n_unit} units")
            if args.epoch_id_length > 0 and len(batch_obj.batch_id_list) > args.epoch:
                break
        if args.epoch_id_length > 0:
            epoch += len(batch_obj.batch_id_list)
        else:
            epoch += 1

    post_mtx = None
    if prior is None:
        lda.feature_names_in_ = feature_kept
        # Relabel factors based on (approximate) descending abundance
        weight = lda.components_.sum(axis=1)
        ordered_k = np.argsort(weight)[::-1]
        lda.components_ = lda.components_[ordered_k,:]
        lda.exp_dirichlet_component_ = lda.exp_dirichlet_component_[ordered_k,:]
        # Store model
        out_f = args.output + ".model.p"
        pickle.dump( lda, open( out_f, "wb" ) )
        post_mtx = lda.components_.T
    else:
        weight = lda._lambda.sum(axis=1)
        post_mtx = lda._lambda.T
    out_f = args.output + ".model_matrix.tsv.gz"
    pd.concat([pd.DataFrame({gene_key: feature_kept}),\
                pd.DataFrame(post_mtx,\
                columns = [str(k) for k in range(K)], dtype='float64')],\
                axis = 1).to_csv(out_f, sep='\t', index=False, float_format='%.4e', compression={"method":"gzip"})

### Rerun all units once and store results
oheader = ["unit",key,"x","y","topK","topP"]+factor_header
dtp = {'topK':int,key:int,"unit":str}
dtp.update({x:float for x in ['topP']+factor_header})
res_f = args.output+".fit_result.tsv.gz"
nbatch = 0
logging.info(f"Result file {res_f}")

ucol = [unit_key,gene_key,key] + unit_attr
if key != train_on:
    ucol += [train_on]
reader = pd.read_csv(gzip.open(args.input, 'rt'), \
        sep='\t',chunksize=chunksize, skiprows=1, names=header, \
        usecols=ucol, dtype=adt)
batch_obj =  UnitLoader(reader, ft_dict, key, \
    batch_id_prefix=args.epoch_id_length, \
    min_ct_per_unit=args.min_ct_per_unit, \
    unit_id=unit_key, unit_attr=unit_attr, train_key=train_on)
post_count = np.zeros((K, M))
while batch_obj.update_batch(b_size):
    N = batch_obj.mtx.shape[0]
    theta = lda.transform(batch_obj.mtx)
    if key != train_on:
        post_count += np.array(theta.T @ batch_obj.test_mtx)
    else:
        post_count += np.array(theta.T @ batch_obj.mtx)

    brc = pd.concat((batch_obj.brc.reset_index(), pd.DataFrame(theta, columns = factor_header)), axis = 1)
    brc['topK'] = np.argmax(theta, axis = 1).astype(int)
    brc['topP'] = np.max(theta, axis = 1)
    brc = brc.astype(dtp)
    logging.info(f"{nbatch}-th batch with {brc.shape[0]} units")
    mod = 'w' if nbatch == 0 else 'a'
    hdr = True if nbatch == 0 else False
    brc[oheader].to_csv(res_f, sep='\t', mode=mod, float_format="%.4e", index=False, header=hdr, compression={"method":"gzip"})
    nbatch += 1
    if args.epoch_id_length > 0 and len(batch_obj.batch_id_list) > 1:
        break

logging.info(f"Finished ({nbatch})")

out_f = args.output+".posterior.count.tsv.gz"
pd.concat([pd.DataFrame({gene_key: feature_kept}),\
        pd.DataFrame(post_count.T, dtype='float64',\
                        columns = [str(k) for k in range(K)])],\
        axis = 1).to_csv(out_f, sep='\t', index=False, float_format='%.2f', compression={"method":"gzip"})