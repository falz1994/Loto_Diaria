#!/usr/bin/env python3
"""Backtest sweep: prueba varias K y combinaciones de pesos para bucket-first.
Guarda `predictions_scores_sweep.csv` con métricas por configuración.
"""
import importlib
import math
import json
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

# preparar DataFrame numérico
dfnum = ext._prepare_numeric_df(df_raw)
N = len(dfnum)
if N < 5:
    print('No hay suficientes datos para backtest (menos de 5 entradas).')
    sys.exit(1)

if N >= 20:
    start = 10
else:
    start = 3
start = min(start, N - 2)

n_numbers = 10

# configuraciones a probar
K_list = [2, 4, 6, 8, 10]
weight_sets = [
    (0.55, 0.35, 0.10),  # baseline
    (0.60, 0.30, 0.10),
    (0.40, 0.50, 0.10),
    (0.70, 0.20, 0.10),
]
min_per_bucket_options = [0, 1]

configs = []
for k in K_list:
    for ws in weight_sets:
        for minb in min_per_bucket_options:
            configs.append({'k': k, 'recency_w': ws[0], 'freq_w': ws[1], 'markov_w': ws[2], 'min_per_bucket': minb})

# also include baseline: anti_freq and group_streaks as configs
configs.append({'k': None, 'baseline': 'anti_freq'})
configs.append({'k': None, 'baseline': 'group_streaks'})

print(f'Backtest sweep: {len(configs)} configuraciones, pasos: {max(0,(len(dfnum)-1)-start+1)}')

results = []

# helper functions

def generate_bucket_first(training_nums, k, recency_w, freq_w, markov_w, n_numbers=10, min_per_bucket=0):
    per_num = ext._analyze_per_number(training_nums, alpha=1, beta=1, decay=0.03)
    N_hist = len(training_nums)
    per_map = {p['number']: p for p in per_num}

    # build buckets
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
        bucket_score = recency_w * bucket_recency + freq_w * (bucket_count / max(1, N_hist)) + markov_w * markov_bucket
        buckets.append({'i': i, 'lo': lo, 'hi': hi, 'nums': bucket_nums, 'count': bucket_count, 'recency': bucket_recency, 'markov': markov_bucket, 'score': bucket_score})
    buckets = sorted(buckets, key=lambda x: x['score'], reverse=True)

    # allocate proportional
    weights = [max(0.0, float(b.get('score', 0.0))) for b in buckets]
    totalw = sum(weights)
    if totalw <= 0:
        weights = [1.0] * len(buckets)
        totalw = float(len(buckets))
    ideals = [w / totalw * n_numbers for w in weights]
    alloc = [int(math.floor(x)) for x in ideals]
    remaining = n_numbers - sum(alloc)
    fracs = sorted([(ideals[i] - alloc[i], i) for i in range(len(buckets))], reverse=True)
    idx = 0
    while remaining > 0 and idx < len(fracs):
        alloc[fracs[idx][1]] += 1
        remaining -= 1
        idx += 1

    if min_per_bucket and len(buckets) <= n_numbers:
        for i in range(len(buckets)):
            if alloc[i] < min_per_bucket:
                alloc[i] = min_per_bucket
        total_alloc = sum(alloc)
        if total_alloc > n_numbers:
            to_reduce = total_alloc - n_numbers
            while to_reduce > 0:
                candidates = [i for i in range(len(buckets)) if alloc[i] > min_per_bucket]
                if not candidates:
                    break
                j = max(candidates, key=lambda x: alloc[x])
                alloc[j] -= 1
                to_reduce -= 1

    chosen = []
    fallback = [int(p['number']) for p in sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)]
    per_map_local = {p['number']: p for p in per_num}

    for i, b in enumerate(buckets):
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
        for n in fallback:
            if len(chosen) >= n_numbers:
                break
            if n not in chosen:
                chosen.append(n)

    return chosen[:n_numbers]

# baselines

def generate_baseline(training_nums, baseline_name, n_numbers=10):
    per_num = ext._analyze_per_number(training_nums, alpha=1, beta=1, decay=0.03)
    if baseline_name == 'anti_freq':
        counts = Counter(training_nums)
        all_numbers = list(range(0, 100))
        anti_list = sorted(all_numbers, key=lambda x: (counts.get(x, 0), x))
        return anti_list[:n_numbers]
    elif baseline_name == 'group_streaks':
        by_heur = sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)
        group0_stats = ext._analyze_boolean_series([v < 50 for v in training_nums])
        group1_stats = ext._analyze_boolean_series([v >= 50 for v in training_nums])
        if group0_stats.get('current_streak', 0) >= 2:
            favored_group = 0
        elif group1_stats.get('current_streak', 0) >= 2:
            favored_group = 1
        else:
            favored_group = 0 if group0_stats.get('pred_bayes', 0.5) >= 0.5 else 1
        group_candidates = [int(x['number']) for x in by_heur if (x['number'] < 50) == (favored_group == 0)]
        return group_candidates[:n_numbers]
    return [0] * n_numbers

# ejecutar sweep
for cfg in configs:
    name = None
    if 'baseline' in cfg:
        name = cfg['baseline']
    else:
        name = f"bucket_k{cfg['k']}_r{int(cfg['recency_w']*100)}_f{int(cfg['freq_w']*100)}_m{int(cfg['markov_w']*100)}_min{cfg['min_per_bucket']}"

    stats = {'total_predictions': 0, 'number_hits': 0, 'group_hits': 0, 'rank_sum': 0, 'rank_hits_count': 0}

    for i in range(start, len(dfnum) - 1):
        training = dfnum.iloc[: i + 1].copy()
        test_row = dfnum.iloc[i + 1]
        training_nums = training['__num'].tolist()
        if 'baseline' in cfg:
            plist = generate_baseline(training_nums, cfg['baseline'], n_numbers=n_numbers)
        else:
            plist = generate_bucket_first(training_nums, cfg['k'], cfg['recency_w'], cfg['freq_w'], cfg['markov_w'], n_numbers=n_numbers, min_per_bucket=cfg['min_per_bucket'])

        actual_num = int(test_row['__num'])
        actual_group = 0 if actual_num < 50 else 1
        grp_count = sum(1 for x in plist if int(x) < 50)
        predicted_group = 0 if grp_count >= (len(plist) / 2) else 1
        hit_number = actual_num in plist
        hit_rank = (plist.index(actual_num) + 1) if hit_number else None
        hit_group = (predicted_group == actual_group)

        stats['total_predictions'] += 1
        if hit_number:
            stats['number_hits'] += 1
            stats['rank_sum'] += int(hit_rank)
            stats['rank_hits_count'] += 1
        if hit_group:
            stats['group_hits'] += 1

    tp = stats['total_predictions']
    nh = stats['number_hits']
    gh = stats['group_hits']
    rs = stats['rank_sum']
    rhc = stats['rank_hits_count']
    wil_low, wil_high = ext._wilson_interval(nh, tp) if tp > 0 else (0.0, 1.0)
    avg_rank = (rs / rhc) if rhc > 0 else None
    results.append({
        'config_name': name,
        'k': cfg.get('k', ''),
        'recency_w': cfg.get('recency_w', ''),
        'freq_w': cfg.get('freq_w', ''),
        'markov_w': cfg.get('markov_w', ''),
        'min_per_bucket': cfg.get('min_per_bucket', ''),
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

# guardar resultados
pd.DataFrame(results).to_csv('predictions_scores_sweep.csv', index=False, encoding='utf-8-sig')
print('Sweep completo. Archivo: predictions_scores_sweep.csv')
