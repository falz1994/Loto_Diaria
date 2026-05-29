#!/usr/bin/env python3
"""Backtest de métodos de predicción usando el histórico `loto_diaria.csv`.
Genera dos archivos de salida:
 - predictions_backtest.csv (una fila por predicción histórica)
 - predictions_scores_new.csv (métricas agregadas por método)

Ejecutar desde la carpeta del proyecto.
"""
import importlib
import math
import json
import random
from collections import Counter
from datetime import datetime
import sys

import pandas as pd

try:
    ext = importlib.import_module('extract_loto_diaria')
except Exception as e:
    print('No se pudo importar extract_loto_diaria:', e)
    raise

CSV_PATH = 'loto_diaria.csv'
try:
    df_raw = pd.read_csv(CSV_PATH, encoding='utf-8-sig', dtype=str)
except Exception:
    df_raw = pd.read_csv(CSV_PATH, dtype=str, encoding='latin1')

# preparar DataFrame numérico (usa la función del módulo principal)
dfnum = ext._prepare_numeric_df(df_raw)
N = len(dfnum)
if N < 5:
    print('No hay suficientes datos para backtest (menos de 5 entradas).')
    sys.exit(1)

# configuración de inicio
if N >= 20:
    start = 10
else:
    start = 3
start = min(start, N - 2)

random.seed(0)  # reproducible

METHOD_NAMES = [
    'bucket_first_k2', 'bucket_first_auto', 'group_streaks', 'buckets_auto', 'buckets_fine',
    'top_freq', 'recency', 'bayes_posterior', 'heuristic_combined',
    'markov_repeat', 'anti_freq', 'hotcold_mix', 'weighted_random',
    # interval-based methods (deterministic and random, 10/20 variants)
    'interval_k10_det_10', 'interval_k10_rand_10', 'interval_k10_det_20', 'interval_k10_rand_20',
    'interval_k20_det_10', 'interval_k20_rand_10', 'interval_k20_det_20', 'interval_k20_rand_20'
]


# estadísticas acumuladas por método
stats = {m: {'total_predictions': 0, 'number_hits': 0, 'group_hits': 0, 'rank_sum': 0, 'rank_hits_count': 0} for m in METHOD_NAMES}
records = []

N_TOTAL_STEPS = max(0, (len(dfnum) - 1) - start + 1)
print(f'Backtest: {N_TOTAL_STEPS} pasos (desde índice {start} hasta {len(dfnum)-2}).')


def fill_list(src, n_numbers=10):
    out = []
    for v in src:
        if len(out) >= n_numbers:
            break
        if v not in out:
            out.append(int(v))
    if len(out) < n_numbers:
        for cand in range(0, 100):
            if cand not in out:
                out.append(cand)
            if len(out) >= n_numbers:
                break
    return out


def generate_methods(training_nums, n_numbers=10):
    # training_nums: list of int
    methods = {}
    N_hist = len(training_nums)
    if N_hist == 0:
        return {m: fill_list([]) for m in METHOD_NAMES}

    per_num = ext._analyze_per_number(training_nums, alpha=1, beta=1, decay=0.03)
    counts = dict(Counter(training_nums))

    by_count = sorted(per_num, key=lambda x: x['count'], reverse=True)
    top_freq = [int(x['number']) for x in by_count]
    by_recency = sorted(per_num, key=lambda x: x['recency_score'], reverse=True)
    recency_list = [int(x['number']) for x in by_recency]
    by_posterior = sorted(per_num, key=lambda x: x['posterior_mean'], reverse=True)
    posterior_list = [int(x['number']) for x in by_posterior]
    by_heur = sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)
    heur_list = [int(x['number']) for x in by_heur]
    by_markov = sorted(per_num, key=lambda x: x.get('markov_repeat_prob', 0.0), reverse=True)
    markov_list = [int(x['number']) for x in by_markov]

    all_numbers = list(range(0, 100))
    counts_full = {i: counts.get(i, 0) for i in all_numbers}
    anti_list = sorted(all_numbers, key=lambda x: (counts_full[x], x))

    # buckets automáticos
    k_auto = max(2, min(10, int(round(math.sqrt(max(1, N_hist))))))
    k_fine = max(2, min(20, int(round(k_auto * 2))))
    per_map = {p['number']: p for p in per_num}

    def bucketize(k):
        bucket_size = int(math.ceil(100.0 / k))
        buckets = []
        for i in range(k):
            lo = i * bucket_size
            hi = min(99, (i + 1) * bucket_size - 1)
            bucket_nums = list(range(lo, hi + 1))
            bucket_count = sum(1 for v in training_nums if lo <= v <= hi)
            bucket_recency = sum(per_map.get(n, {}).get('recency_score', 0.0) for n in bucket_nums)
            transitions_from = 0
            transitions_same = 0
            for a, b in zip(training_nums[:-1], training_nums[1:]):
                if lo <= a <= hi:
                    transitions_from += 1
                    if lo <= b <= hi:
                        transitions_same += 1
            markov_bucket = (transitions_same / transitions_from) if transitions_from > 0 else 0.0
            bucket_score = 0.55 * bucket_recency + 0.35 * (bucket_count / max(1, N_hist)) + 0.10 * markov_bucket
            buckets.append({'i': i, 'lo': lo, 'hi': hi, 'nums': bucket_nums, 'count': bucket_count, 'recency': bucket_recency, 'markov': markov_bucket, 'score': bucket_score})
        buckets = sorted(buckets, key=lambda x: x['score'], reverse=True)
        return buckets

    buckets_auto = bucketize(k_auto)
    buckets_fine = bucketize(k_fine)

    # heurística de rachas por grupo
    group0_stats = ext._analyze_boolean_series([v < 50 for v in training_nums])
    group1_stats = ext._analyze_boolean_series([v >= 50 for v in training_nums])
    if group0_stats.get('current_streak', 0) >= 2:
        favored_group = 0
    elif group1_stats.get('current_streak', 0) >= 2:
        favored_group = 1
    else:
        favored_group = 0 if group0_stats.get('pred_bayes', 0.5) >= 0.5 else 1

    group_candidates = [int(x['number']) for x in by_heur if (x['number'] < 50) == (favored_group == 0)]
    group_candidates = group_candidates[:n_numbers]

    def candidates_from_buckets(buckets_list):
        out = []
        for b in buckets_list:
            inside = [n for n in [p['number'] for p in by_heur] if b['lo'] <= n <= b['hi']]
            for n in b['nums']:
                if n not in inside:
                    inside.append(n)
            for n in inside:
                if n not in out:
                    out.append(n)
                if len(out) >= n_numbers:
                    break
            if len(out) >= n_numbers:
                break
        return out

    buckets_auto_list = candidates_from_buckets(buckets_auto)
    buckets_fine_list = candidates_from_buckets(buckets_fine)

    # allocation: distribuir n_numbers proporcionalmente entre buckets
    def _allocate_and_select(buckets_list, n_numbers=10, min_per_bucket=0):
        k = len(buckets_list)
        if k == 0:
            return []
        weights = [max(0.0, float(b.get('score', 0.0))) for b in buckets_list]
        totalw = sum(weights)
        if totalw <= 0:
            weights = [1.0] * k
            totalw = float(k)
        ideals = [w / totalw * n_numbers for w in weights]
        alloc = [int(math.floor(x)) for x in ideals]
        remaining = n_numbers - sum(alloc)
        fracs = sorted([(ideals[i] - alloc[i], i) for i in range(k)], reverse=True)
        idx = 0
        while remaining > 0 and idx < len(fracs):
            alloc[fracs[idx][1]] += 1
            remaining -= 1
            idx += 1
        if min_per_bucket and k <= n_numbers:
            for i in range(k):
                if alloc[i] < min_per_bucket:
                    alloc[i] = min_per_bucket
            total_alloc = sum(alloc)
            if total_alloc > n_numbers:
                to_reduce = total_alloc - n_numbers
                while to_reduce > 0:
                    candidates = [i for i in range(k) if alloc[i] > min_per_bucket]
                    if not candidates:
                        break
                    j = max(candidates, key=lambda x: alloc[x])
                    alloc[j] -= 1
                    to_reduce -= 1
        chosen = []
        per_map_local = {p['number']: p for p in per_num}
        for i, b in enumerate(buckets_list):
            want = alloc[i]
            if want <= 0:
                continue
            bucket_nums = b.get('nums', [])
            bucket_sorted = sorted(bucket_nums, key=lambda n: per_map_local.get(int(n), {}).get('heuristic_score', 0.0), reverse=True)
            picked = []
            for n in bucket_sorted:
                if len(picked) >= want:
                    break
                if n not in chosen:
                    picked.append(int(n))
            for n in bucket_nums:
                if len(picked) >= want:
                    break
                if n not in chosen and n not in picked:
                    picked.append(int(n))
            chosen.extend(picked)
        if len(chosen) < n_numbers:
            fallback = [int(p['number']) for p in sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)]
            for n in fallback:
                if len(chosen) >= n_numbers:
                    break
                if n not in chosen:
                    chosen.append(n)
        return chosen[:n_numbers]

    # bucket-first candidates
    buckets_k2 = bucketize(2)
    bucket_first_k2 = _allocate_and_select(buckets_k2, n_numbers=n_numbers, min_per_bucket=1 if len(buckets_k2) <= n_numbers else 0)
    bucket_first_auto = _allocate_and_select(buckets_auto, n_numbers=n_numbers, min_per_bucket=1 if len(buckets_auto) <= n_numbers else 0)

    # weighted random (sin reemplazo) basado en recency+frequency
    weights = {}
    for p in per_num:
        weights[p['number']] = max(0.0001, p.get('recency_score', 0.0) + p.get('frequency', 0.0))
    pool = list(weights.keys())
    chosen = []
    if pool:
        import random as _rnd
        wlist = [weights[k] for k in pool]
        while len(chosen) < n_numbers and pool:
            r = _rnd.random() * sum(wlist)
            cum = 0
            pick = None
            for j, w in enumerate(wlist):
                cum += w
                if r <= cum:
                    pick = pool[j]
                    break
            if pick is None:
                pick = pool[-1]
            chosen.append(int(pick))
            idx = pool.index(pick)
            pool.pop(idx)
            wlist.pop(idx)

    def _interval_buckets(k):
        buckets = []
        num_buckets = int(math.ceil(100.0 / k))
        for i in range(num_buckets):
            lo = i * k
            hi = min(99, (i + 1) * k - 1)
            bucket_nums = list(range(lo, hi + 1))
            bucket_count = sum(1 for v in training_nums if lo <= v <= hi)
            bucket_recency = sum(per_map.get(n, {}).get('recency_score', 0.0) for n in bucket_nums)
            transitions_from = 0
            transitions_same = 0
            for a, b in zip(training_nums[:-1], training_nums[1:]):
                if lo <= a <= hi:
                    transitions_from += 1
                    if lo <= b <= hi:
                        transitions_same += 1
            markov_bucket = (transitions_same / transitions_from) if transitions_from > 0 else 0.0
            bucket_score = 0.55 * bucket_recency + 0.35 * (bucket_count / max(1, N_hist)) + 0.10 * markov_bucket
            buckets.append({'i': i, 'lo': lo, 'hi': hi, 'nums': bucket_nums, 'count': bucket_count, 'recency': bucket_recency, 'markov': markov_bucket, 'score': bucket_score})
        buckets = sorted(buckets, key=lambda x: x['score'], reverse=True)
        return buckets

    def _interval_det_candidates(k, want):
        buckets = _interval_buckets(k)
        per_map_local = {p['number']: p for p in per_num}
        chosen = []
        top_intervals = 1
        for b in buckets[:top_intervals]:
            bucket_sorted = sorted(b['nums'], key=lambda n: per_map_local.get(int(n), {}).get('heuristic_score', 0.0), reverse=True)
            for n in bucket_sorted:
                if len(chosen) >= want:
                    break
                if n not in chosen:
                    chosen.append(int(n))
            if len(chosen) >= want:
                break
        if len(chosen) < want:
            fallback = [int(p['number']) for p in sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)]
            for n in fallback:
                if len(chosen) >= want:
                    break
                if n not in chosen:
                    chosen.append(n)
        return chosen[:want]

    def _interval_rand_candidates(k, want):
        buckets = _interval_buckets(k)
        pool = []
        for b in buckets[:1]:
            for n in b['nums']:
                if n not in pool:
                    pool.append(n)
        if not pool:
            return fill_list([], want)
        weights_local = []
        for n in pool:
            weights_local.append(max(0.0001, per_map.get(n, {}).get('recency_score', 0.0) + counts_full.get(n, 0)))
        import random as _rnd
        chosen = []
        pool_copy = pool[:]
        wlist = weights_local[:]
        while len(chosen) < want and pool_copy:
            r = _rnd.random() * sum(wlist)
            cum = 0
            pick = None
            for j, w in enumerate(wlist):
                cum += w
                if r <= cum:
                    pick = pool_copy[j]
                    break
            if pick is None:
                pick = pool_copy[-1]
            chosen.append(int(pick))
            idx = pool_copy.index(pick)
            pool_copy.pop(idx)
            wlist.pop(idx)
        if len(chosen) < want:
            fallback = [int(p['number']) for p in sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)]
            for n in fallback:
                if len(chosen) >= want:
                    break
                if n not in chosen:
                    chosen.append(n)
        return chosen[:want]

    methods = {
        'bucket_first_k2': fill_list(bucket_first_k2, n_numbers),
        'bucket_first_auto': fill_list(bucket_first_auto, n_numbers),
        'group_streaks': fill_list(group_candidates, n_numbers),
        'buckets_auto': fill_list(buckets_auto_list, n_numbers),
        'buckets_fine': fill_list(buckets_fine_list, n_numbers),
        'top_freq': fill_list(top_freq, n_numbers),
        'recency': fill_list(recency_list, n_numbers),
        'bayes_posterior': fill_list(posterior_list, n_numbers),
        'heuristic_combined': fill_list(heur_list, n_numbers),
        'markov_repeat': fill_list(markov_list if any(x.get('markov_repeat_prob', 0) > 0 for x in per_num) else heur_list, n_numbers),
        'anti_freq': fill_list(anti_list, n_numbers),
        'hotcold_mix': fill_list([int(x['number']) for x in by_count[:5]] + anti_list[:5], n_numbers),
        'weighted_random': fill_list(chosen, n_numbers),
        # interval-based deterministic/rand (k=10)
        'interval_k10_det_10': _interval_det_candidates(10, n_numbers),
        'interval_k10_rand_10': _interval_rand_candidates(10, n_numbers),
        'interval_k10_det_20': _interval_det_candidates(10, 20),
        'interval_k10_rand_20': _interval_rand_candidates(10, 20),
        # interval-based deterministic/rand (k=20)
        'interval_k20_det_10': _interval_det_candidates(20, n_numbers),
        'interval_k20_rand_10': _interval_rand_candidates(20, n_numbers),
        'interval_k20_det_20': _interval_det_candidates(20, 20),
        'interval_k20_rand_20': _interval_rand_candidates(20, 20),
    }

    return methods


# ejecutar simulación secuencial
for i in range(start, len(dfnum) - 1):
    training = dfnum.iloc[: i + 1].copy()
    test_row = dfnum.iloc[i + 1]
    training_nums = training['__num'].tolist()
    methods_preds = generate_methods(training_nums, n_numbers=10)
    actual_num = int(test_row['__num'])
    actual_group = 0 if actual_num < 50 else 1

    for mname, plist in methods_preds.items():
        grp_count = sum(1 for x in plist if int(x) < 50)
        predicted_group = 0 if grp_count >= (len(plist) / 2) else 1
        hit_number = actual_num in plist
        hit_rank = (plist.index(actual_num) + 1) if hit_number else None
        hit_group = (predicted_group == actual_group)

        stats[mname]['total_predictions'] += 1
        if hit_number:
            stats[mname]['number_hits'] += 1
            stats[mname]['rank_sum'] += int(hit_rank)
            stats[mname]['rank_hits_count'] += 1
        if hit_group:
            stats[mname]['group_hits'] += 1

        records.append({
            'predicted_for_sorteo': int(test_row['Sorteo']),
            'method': mname,
            'predicted_numbers': json.dumps(plist),
            'predicted_group': int(predicted_group),
            'predicted_at': datetime.utcnow().isoformat(),
            'evaluated': True,
            'evaluated_at': datetime.utcnow().isoformat(),
            'actual_sorteo': int(test_row['Sorteo']),
            'actual_number': int(actual_num),
            'hit_number': bool(hit_number),
            'hit_group': bool(hit_group),
            'hit_rank': int(hit_rank) if hit_rank is not None else ''
        })

# guardar resultados detallados
pd.DataFrame(records).to_csv('predictions_backtest.csv', index=False, encoding='utf-8-sig')

# agregar métricas por método
rows = []
for m, v in stats.items():
    tp = v['total_predictions']
    nh = v['number_hits']
    gh = v['group_hits']
    rs = v['rank_sum']
    rhc = v['rank_hits_count']
    wil_low, wil_high = ext._wilson_interval(nh, tp) if tp > 0 else (0.0, 1.0)
    avg_rank = (rs / rhc) if rhc > 0 else None
    rows.append({
        'method': m,
        'total_predictions': tp,
        'number_hits': nh,
        'group_hits': gh,
        'rank_sum': rs,
        'rank_hits_count': rhc,
        'hit_rate': nh / tp if tp > 0 else None,
        'group_hit_rate': gh / tp if tp > 0 else None,
        'avg_rank': avg_rank,
        'wilson_low': wil_low,
        'wilson_high': wil_high,
        'last_update': datetime.utcnow().isoformat()
    })

df_scores = pd.DataFrame(rows)
df_scores.to_csv('predictions_scores_new.csv', index=False, encoding='utf-8-sig')
print('Backtest completo. Archivos: predictions_backtest.csv, predictions_scores_new.csv')
