from pkg_resources import resource_filename
import pandas as pd
import numpy as np
from pyfaidx import Fasta
from keras.models import load_model
import logging
from sys import exit

INFO_FIELD_KEYS = [
    'ALLELE',
    'NAME',
    'STRAND',
    'DS_AG',
    'DS_AL',
    'DS_DG',
    'DS_DL',
    'DP_AG',
    'DP_AL',
    'DP_DG',
    'DP_DL',
    'DS_AG_REF',
    'DS_AG_ALT',
    'DS_AL_REF',
    'DS_AL_ALT',
    'DS_DG_REF',
    'DS_DG_ALT',
    'DS_DL_REF',
    'DS_DL_ALT',
]


class Annotator:

    def __init__(self, ref_fasta, annotations):

        if annotations == 'grch37':
            annotations = resource_filename(__name__, 'annotations/grch37.txt')
        elif annotations == 'grch38':
            annotations = resource_filename(__name__, 'annotations/grch38.txt')

        try:
            df = pd.read_csv(annotations, sep='\t', dtype={'CHROM': object})
            self.genes = df['#NAME'].to_numpy()
            self.chroms = df['CHROM'].to_numpy()
            self.strands = df['STRAND'].to_numpy()
            self.tx_starts = df['TX_START'].to_numpy()+1
            self.tx_ends = df['TX_END'].to_numpy()
            self.exon_starts = [np.asarray([int(i) for i in c.split(',') if i])+1
                                for c in df['EXON_START'].to_numpy()]
            self.exon_ends = [np.asarray([int(i) for i in c.split(',') if i])
                              for c in df['EXON_END'].to_numpy()]
        except IOError as e:
            logging.error('{}'.format(e))
            exit()
        except (KeyError, pd.errors.ParserError) as e:
            logging.error('Gene annotation file {} not formatted properly: {}'.format(annotations, e))
            exit()

        try:
            self.ref_fasta = Fasta(ref_fasta, rebuild=False)
        except IOError as e:
            logging.error('{}'.format(e))
            exit()

        paths = ('models/spliceai{}.h5'.format(x) for x in range(1, 6))
        self.models = [load_model(resource_filename(__name__, x)) for x in paths]

    def get_name_and_strand(self, chrom, pos):

        chrom = normalise_chrom(chrom, list(self.chroms)[0])
        idxs = np.intersect1d(np.nonzero(self.chroms == chrom)[0],
                              np.intersect1d(np.nonzero(self.tx_starts <= pos)[0],
                              np.nonzero(pos <= self.tx_ends)[0]))

        if len(idxs) >= 1:
            return self.genes[idxs], self.strands[idxs], idxs
        else:
            return [], [], []

    def get_pos_data(self, idx, pos):

        dist_tx_start = self.tx_starts[idx]-pos
        dist_tx_end = self.tx_ends[idx]-pos
        dist_exon_bdry = min(np.union1d(self.exon_starts[idx], self.exon_ends[idx])-pos, key=abs)
        dist_ann = (dist_tx_start, dist_tx_end, dist_exon_bdry)

        return dist_ann


def one_hot_encode(seq):

    map = np.asarray([[0, 0, 0, 0],
                      [1, 0, 0, 0],
                      [0, 1, 0, 0],
                      [0, 0, 1, 0],
                      [0, 0, 0, 1]])

    seq = seq.upper().replace('A', '\x01').replace('C', '\x02')
    seq = seq.replace('G', '\x03').replace('T', '\x04').replace('N', '\x00')

    return map[np.fromstring(seq, np.int8) % 5]


def normalise_chrom(source, target):

    def has_prefix(x):
        return x.startswith('chr')

    if has_prefix(source) and not has_prefix(target):
        return source.strip('chr')
    elif not has_prefix(source) and has_prefix(target):
        return 'chr'+source

    return source


def get_delta_scores_for_transcript(x_ref, x_alt, ref_len, alt_len, strand, cov, ann):
    del_len = max(ref_len-alt_len, 0)

    x_ref = one_hot_encode(x_ref)[None, :]
    x_alt = one_hot_encode(x_alt)[None, :]

    if strand == '-':
        x_ref = x_ref[:, ::-1, ::-1]
        x_alt = x_alt[:, ::-1, ::-1]

    y_ref = np.mean([ann.models[m].predict(x_ref) for m in range(5)], axis=0)
    y_alt = np.mean([ann.models[m].predict(x_alt) for m in range(5)], axis=0)

    if strand == '-':
        y_ref = y_ref[:, ::-1]
        y_alt = y_alt[:, ::-1]

    if ref_len > 1 and alt_len == 1:
        y_alt = np.concatenate([
            y_alt[:, :cov//2+alt_len],
            np.zeros((1, del_len, 3)),
            y_alt[:, cov//2+alt_len:]],
            axis=1)
    elif ref_len == 1 and alt_len > 1:
        y_alt = np.concatenate([
            y_alt[:, :cov//2],
            np.max(y_alt[:, cov//2:cov//2+alt_len], axis=1)[:, None, :],
            y_alt[:, cov//2+alt_len:]],
            axis=1)
    #MNP handling
    elif ref_len > 1 and alt_len > 1:
        zblock = np.zeros((1,ref_len-1,3))
        y_alt = np.concatenate([
            y_alt[:, :cov//2],
            np.max(y_alt[:, cov//2:cov//2+alt_len], axis=1)[:, None, :],
            zblock,
            y_alt[:, cov//2+alt_len:]],
            axis=1)

    return y_ref, y_alt



def get_delta_scores(record, ann, dist_var, mask):

    cov = 2*dist_var+1
    wid = 10000+cov
    scores = []

    try:
        record.chrom, record.pos, record.ref, len(record.alts)
    except TypeError:
        logging.warning('Skipping record (bad input): {}'.format(record))
        return scores

    (genes, strands, idxs) = ann.get_name_and_strand(record.chrom, record.pos)
    if len(idxs) == 0:
        return scores

    chrom = normalise_chrom(record.chrom, list(ann.ref_fasta.keys())[0])
    try:
        seq = ann.ref_fasta[chrom][record.pos-wid//2-1:record.pos+wid//2].seq
    except (IndexError, ValueError):
        logging.warning('Skipping record (fasta issue): {}'.format(record))
        return scores

    if seq[wid//2:wid//2+len(record.ref)].upper() != record.ref:
        logging.warning('Skipping record (ref issue): {}'.format(record))
        return scores

    if len(seq) != wid:
        logging.warning('Skipping record (near chromosome end): {}'.format(record))
        return scores

    if len(record.ref) > 2*dist_var:
        logging.warning('Skipping record (ref too long): {}'.format(record))
        return scores

    genomic_coords = np.arange(record.pos-cov//2, record.pos+cov//2 + 1)

    # many of the transcripts in a gene can have the same tx start & stop positions, so their results can be cached
    # since SpliceAI scores (prior to masking) depend only on transcript start & stop coordinates and strand.
    delta_scores_transcript_cache = {}
    model_prediction_count = 0
    total_count = 0
    for j in range(len(record.alts)):
        for i in range(len(idxs)):

            if '.' in record.alts[j] or '-' in record.alts[j] or '*' in record.alts[j]:
                continue

            if '<' in record.alts[j] or '>' in record.alts[j]:
                continue

            dist_ann = ann.get_pos_data(idxs[i], record.pos)
            pad_size = [max(wid//2+dist_ann[0], 0), max(wid//2-dist_ann[1], 0)]
            ref_len = len(record.ref)
            alt_len = len(record.alts[j])

            x_ref = 'N'*pad_size[0]+seq[pad_size[0]:wid-pad_size[1]]+'N'*pad_size[1]
            x_alt = x_ref[:wid//2]+str(record.alts[j])+x_ref[wid//2+ref_len:]

            total_count += 1
            strand = strands[i]
            args = (x_ref, x_alt, ref_len, alt_len, strand, cov)
            if args not in delta_scores_transcript_cache:
                model_prediction_count += 1
            delta_scores_transcript_cache[args] = get_delta_scores_for_transcript(*args, ann=ann)

            y_ref, y_alt = delta_scores_transcript_cache[args]

            y = np.concatenate([y_ref, y_alt])

            idx_pa = (y[1, :, 1]-y[0, :, 1]).argmax()
            idx_na = (y[0, :, 1]-y[1, :, 1]).argmax()
            idx_pd = (y[1, :, 2]-y[0, :, 2]).argmax()
            idx_nd = (y[0, :, 2]-y[1, :, 2]).argmax()

            mask_pa = np.logical_and((idx_pa-cov//2 == dist_ann[2]), mask)
            mask_na = np.logical_and((idx_na-cov//2 != dist_ann[2]), mask)
            mask_pd = np.logical_and((idx_pd-cov//2 == dist_ann[2]), mask)
            mask_nd = np.logical_and((idx_nd-cov//2 != dist_ann[2]), mask)

            if len(genomic_coords) != y_ref.shape[1]:
                raise ValueError(f"SpliceAI internal error: len(genomic_coords) != y_ref.shape[1]: "
                                 f"{len(genomic_coords)} != {y_ref.shape[1]}")

            if len(genomic_coords) != y_alt.shape[1]:
                raise ValueError(f"SpliceAI internal error: len(genomic_coords) != y_alt.shape[1]: "
                                 f"{len(genomic_coords)} != {y_alt.shape[1]}")

            scores.append({
                "ALLELE": record.alts[j],
                "NAME": genes[i],
                "STRAND": strands[i],
                "DS_AG": float((y[1, idx_pa, 1]-y[0, idx_pa, 1])*(1-mask_pa)),
                "DS_AL": float((y[0, idx_na, 1]-y[1, idx_na, 1])*(1-mask_na)),
                "DS_DG": float((y[1, idx_pd, 2]-y[0, idx_pd, 2])*(1-mask_pd)),
                "DS_DL": float((y[0, idx_nd, 2]-y[1, idx_nd, 2])*(1-mask_nd)),
                "DP_AG": int(idx_pa-cov//2),
                "DP_AL": int(idx_na-cov//2),
                "DP_DG": int(idx_pd-cov//2),
                "DP_DL": int(idx_nd-cov//2),
                "DS_AG_REF": float(y[0, idx_pa, 1]),
                "DS_AL_REF": float(y[0, idx_na, 1]),
                "DS_DG_REF": float(y[0, idx_pd, 2]),
                "DS_DL_REF": float(y[0, idx_nd, 2]),
                "DS_AG_ALT": float(y[1, idx_pa, 1]),
                "DS_AL_ALT": float(y[1, idx_na, 1]),
                "DS_DG_ALT": float(y[1, idx_pd, 2]),
                "DS_DL_ALT": float(y[1, idx_nd, 2]),
                "ALL_NON_ZERO_SCORES": [
                    {
                        "pos": int(genomic_coord),
                        "RA": float(ref_acceptor_score),
                        "AA": float(alt_acceptor_score),
                        "RD": float(ref_donor_score),
                        "AD": float(alt_donor_score),
                    } for i, (genomic_coord, ref_acceptor_score, alt_acceptor_score, ref_donor_score, alt_donor_score) in enumerate(zip(
                        genomic_coords, y_ref[0, :, 1], y_alt[0, :, 1], y_ref[0, :, 2], y_alt[0, :, 2])
                    ) if any(score >= 0.01 for score in (ref_acceptor_score, alt_acceptor_score, ref_donor_score, ref_acceptor_score))
                         or i in (idx_pa, idx_na, idx_pd, idx_nd)
                ],
            })

    #print(f"Done computing scores. Hit cache for {total_count - model_prediction_count:,d} out of {total_count:,d} transcripts")

    return scores

