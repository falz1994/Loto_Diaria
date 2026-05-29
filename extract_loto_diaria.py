#!/usr/bin/env python3
"""
Extrae filas con 'Loto Diaria' desde la web, convierte la fecha a formato EEUU
(M/D/YYYY), separa la hora en columna `Hora` y guarda CSV/Excel.

Uso:
    python extract_loto_diaria.py
"""
from pathlib import Path
import argparse
import re
import unicodedata
import requests
from bs4 import BeautifulSoup
import pandas as pd
import math
from collections import Counter
import json
from datetime import datetime
import os
import logging
import time
import signal
import sys
from logging.handlers import RotatingFileHandler

# logging setup with rotation
LOG_FILE = os.environ.get('LOTO_LOG', 'extract_loto_diaria.log')
handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
handler.setLevel(logging.INFO)
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# current lock file in use (for signal handler)
CURRENT_LOCKFILE = None

PREDICTION_NUMBERS_PER_ROW = 10
COST_PER_NUMBER = 5
PAYOUT_PER_HIT = 300


def acquire_lock(lockfile='extract_loto_diaria.lock', timeout=3600):
    """Acquire a simple pid-based lockfile. Returns True if acquired, False otherwise."""
    lock_path = Path(lockfile)
    pid = os.getpid()
    now = time.time()
    global CURRENT_LOCKFILE
    try:
        # atomic create
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(f"{pid},{int(now)}")
        CURRENT_LOCKFILE = lockfile
        logger.info('Lock acquired (%s) pid=%s', lockfile, pid)
        return True
    except FileExistsError:
        try:
            txt = lock_path.read_text()
            parts = txt.split(',')
            stored_pid = int(parts[0]) if parts and parts[0].isdigit() else None
            stored_ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        except Exception:
            stored_pid = None
            stored_ts = 0

        # stale if older than timeout
        try:
            if lock_path.exists() and (time.time() - lock_path.stat().st_mtime) > timeout:
                try:
                    lock_path.unlink()
                except Exception:
                    pass
                return acquire_lock(lockfile, timeout)
        except Exception:
            pass

        if stored_pid:
            try:
                os.kill(stored_pid, 0)
                # process alive
                logger.info('Lock exists and process %s alive; cannot acquire', stored_pid)
                return False
            except Exception:
                # process not alive -> stale
                try:
                    lock_path.unlink()
                except Exception:
                    pass
                return acquire_lock(lockfile, timeout)
        return False


def release_lock(lockfile='extract_loto_diaria.lock'):
    lock_path = Path(lockfile)
    try:
        if not lock_path.exists():
            return
        txt = lock_path.read_text()
        stored_pid = int(txt.split(',')[0]) if txt else None
        if stored_pid is None or stored_pid == os.getpid():
            try:
                lock_path.unlink()
                logger.info('Lock released (%s)', lockfile)
            except Exception:
                pass
    except Exception:
        pass


def _on_terminate(signum, frame):
    logger.info('Received signal %s, releasing lock and exiting', signum)
    # try to remove default lock if present
    try:
        if CURRENT_LOCKFILE:
            release_lock(CURRENT_LOCKFILE)
        else:
            release_lock()
    except Exception:
        pass
    sys.exit(1)



def _strip_accents(s: str) -> str:
    return unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('ASCII')


def parse_spanish_date(fecha_raw: str) -> str:
    """Convierte '05 de Mayo 2026 - Martes' -> '5/5/2026'. Si no se puede parsear,
    devuelve la cadena original."""
    if not fecha_raw:
        return ''
    fecha_raw = fecha_raw.strip()
    # aceptar opcionalmente una coma entre mes y año (p.ej. '09 de Mayo, 2026')
    m = re.search(r"(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóúÑñ]+),?\s*(\d{4})", fecha_raw, re.IGNORECASE)
    months = {
        'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
        'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
    }
    if m:
        day = int(m.group(1))
        month_name = _strip_accents(m.group(2).lower())
        # eliminar caracteres no alfabéticos (comas, espacios, puntos)
        month_name = re.sub(r'[^a-z]', '', month_name)
        year = int(m.group(3))
        # match full month name (allow truncated/abbrev)
        for name, num in months.items():
            if month_name.startswith(name[:3]):
                month = num
                return f"{month}/{day}/{year}"
        # fallback try direct lookup
        if month_name in months:
            month = months[month_name]
            return f"{month}/{day}/{year}"
    # soportar formatos como MM/DD/YYYY o M/D/YYYY
    m2 = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", fecha_raw)
    if m2:
        # asumimos que está en formato M/D/YYYY o D/M/YYYY; preferimos M/D/YYYY
        month = int(m2.group(1))
        day = int(m2.group(2))
        year = int(m2.group(3))
        return f"{month}/{day}/{year}"
    return fecha_raw


def normalize_time(time_raw: str) -> str:
    """Normaliza tiempos como '12PM' o '3PM' a '12:00 PM' / '3:00 PM'."""
    if not time_raw:
        return ''
    s = time_raw.strip().upper().replace('.', '')
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?", s)
    if not m:
        return time_raw
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm:
        return f"{hour}:{minute:02d} {ampm}"
    # si no se indica AM/PM devolvemos con minutos (no asumimos)
    return f"{hour}:{minute:02d}"


def fetch_table_from_web(url="https://www.yelu.com.ni/lottery/results/history"):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; extract_loto_diaria/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"Advertencia: no se pudo obtener la página web: {e}")
        return None
    soup = BeautifulSoup(resp.text, 'html.parser')
    desc = soup.find('div', class_='desc')
    if not desc:
        return None
    table = desc.find('table')
    return table


def fetch_records_from_loto_site(url="https://loto.com.ni/diaria/"):
    """Robust fallback scraper for https://loto.com.ni/diaria/.

    Tries several strategies to locate the blocks shown in the sample HTML:
    - direct selector for `div.Rtable.Rtable--2cols.Rtable--collapse`
    - children of `div.listingTable`
    - parents of `div` elements with class `Rtable-cell--head`
    - ascend from `span.spanAzul` elements

    Returns a list of records with keys:
      'Fecha del Sorteo', 'Juega', 'Hora', 'Números Ganadores', 'Sorteo'
    or None on failure.
    """
    # allow forcing a simulated fallback failure for testing
    if os.environ.get('LOTO_FORCE_FALLBACK_FAIL', '').lower() in ('1', 'true', 'yes'):
        logger.info('Simulando fallo del fallback por LOTO_FORCE_FALLBACK_FAIL')
        return None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)'
                        ' Chrome/114.0.0.0 Safari/537.36',
        'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
        'Referer': 'https://loto.com.ni/'
    }
    sess = requests.Session()
    try:
        resp = sess.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("No se pudo obtener %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    def class_contains(tag, substr):
        cls = tag.get('class')
        if not cls:
            return False
        if isinstance(cls, list):
            return any(substr in c for c in cls)
        return substr in cls

    # strategy 1: CSS selector (fast)
    blocks = soup.select('div.Rtable.Rtable--2cols.Rtable--collapse') or []

    # strategy 2: children of listingTable
    if not blocks:
        listing = soup.find('div', class_=lambda c: c and ('listingTable' in (c if isinstance(c, str) else ' '.join(c))))
        if listing:
            blocks = [d for d in listing.find_all('div', recursive=False) if class_contains(d, 'Rtable') or class_contains(d, 'Rtable--2cols')]
            # if direct children didn't match, look deeper
            if not blocks:
                blocks = [d for d in listing.find_all('div') if class_contains(d, 'Rtable')]

    # strategy 3: find head cells and use their parent blocks
    if not blocks:
        head_divs = [d for d in soup.find_all('div') if class_contains(d, 'Rtable-cell--head')]
        parents = []
        for h in head_divs:
            if h and h.parent:
                parents.append(h.parent)
        # deduplicate while preserving order
        seen = set()
        blocks = []
        for p in parents:
            pid = id(p)
            if pid not in seen:
                seen.add(pid)
                blocks.append(p)

    # strategy 4: ascend from span.spanAzul
    if not blocks:
        span_heads = soup.find_all('span', class_=lambda c: c and ('spanAzul' in (c if isinstance(c, str) else ' '.join(c))))
        parents = []
        for s in span_heads:
            p = s
            for _ in range(6):
                p = getattr(p, 'parent', None)
                if not p:
                    break
                if class_contains(p, 'Rtable') or class_contains(p, 'Rtable--2cols'):
                    parents.append(p)
                    break
        # deduplicate
        seen = set(); blocks = []
        for p in parents:
            pid = id(p)
            if pid not in seen:
                seen.add(pid); blocks.append(p)

    if not blocks:
        # Algunos sitios inyectan el HTML dentro de un array JS (e.g. var objJson = [{adName: "<div...>"},...])
        # Intentar extraer las cadenas HTML embebidas en adName dentro del JS y parsearlas.
        try:
            ad_matches = re.findall(r'adName\s*:\s*"(.*?)"', resp.text, flags=re.S)
        except Exception:
            ad_matches = []

        if ad_matches:
            # intentar parsear directamente las cadenas HTML embebidas (adName)
            records_from_js = []
            for snip in ad_matches:
                try:
                    bs = BeautifulSoup(snip, 'html.parser')
                    # localizar head y data dentro del snippet (mismo enfoque que en debug)
                    head = bs.find('div', class_='Rtable-cell Rtable-cell--head') or bs.find('div', class_='Rtable-cell--head')
                    data = bs.find('div', class_='Rtable-cell border-left-td') or bs.find('div', class_='border-left-td')
                    if not head or not data:
                        # intentar buscar dentro de cualquier div hijo
                        for d in bs.find_all('div'):
                            hd = d.find('div', class_=lambda c: c and 'Rtable-cell--head' in (c if isinstance(c, str) else ' '.join(c)))
                            dt = d.find('div', class_=lambda c: c and ('border-left-td' in (c if isinstance(c, str) else ' '.join(c)) or 'Rtable-cell' in (c if isinstance(c, str) else ' '.join(c))))
                            if hd and dt:
                                head = hd; data = dt; break
                    if not head or not data:
                        continue

                    span_azuls = head.find_all('span', class_=lambda c: c and ('spanAzul' in (c if isinstance(c, str) else ' '.join(c))))
                    fecha_raw = span_azuls[0].get_text(' ', strip=True) if span_azuls else head.get_text(' ', strip=True)
                    hora_raw = span_azuls[1].get_text(strip=True) if len(span_azuls) > 1 else ''
                    fecha_us = parse_spanish_date(fecha_raw)
                    span_negro = head.find('span', class_=lambda c: c and ('spanNegro' in (c if isinstance(c, str) else ' '.join(c))))
                    sorteo_raw = span_negro.get_text(' ', strip=True) if span_negro else ''

                    tokens = [s for s in data.stripped_strings]
                    nums = []
                    for t in tokens:
                        m = re.search(r"(\d+)", t)
                        if m:
                            nums.append(m.group(1))
                    if len(nums) >= 2:
                        numeros = f"{nums[0]}{nums[1]}"
                    elif len(nums) == 1:
                        numeros = nums[0]
                    else:
                        numeros = ' '.join(tokens)

                    sorteo = re.sub(r'^\s*#', '', sorteo_raw).strip()
                    records_from_js.append({
                        'Fecha del Sorteo': fecha_us,
                        'Juega': 'Loto Diaria',
                        'Hora': normalize_time(hora_raw),
                        'Números Ganadores': numeros,
                        'Sorteo': sorteo,
                    })
                except Exception:
                    continue
            if records_from_js:
                return records_from_js
        else:
            logger.info('No se encontraron bloques tipo Rtable en %s', url)
            return None

    records = []
    for block in blocks:
        # try to locate head and data cells robustly
        head = None
        data = None
        for child in block.find_all('div', recursive=False):
            if class_contains(child, 'Rtable-cell--head') or class_contains(child, 'Rtable-cell') and 'head' in (" ".join(child.get('class') if child.get('class') else [])):
                head = child
            elif class_contains(child, 'border-left-td') or (class_contains(child, 'Rtable-cell') and not class_contains(child, 'Rtable-cell--head')):
                data = child
        # fallback: search anywhere inside block
        if not head:
            head = block.find('div', class_=lambda c: c and 'Rtable-cell--head' in (c if isinstance(c, str) else ' '.join(c)))
        if not data:
            data = block.find('div', class_=lambda c: c and ('border-left-td' in (c if isinstance(c, str) else ' '.join(c)) or 'Rtable-cell' in (c if isinstance(c, str) else ' '.join(c))))

        if not head or not data:
            continue

        span_azuls = head.find_all('span', class_=lambda c: c and ('spanAzul' in (c if isinstance(c, str) else ' '.join(c))))
        fecha_raw = span_azuls[0].get_text(" ", strip=True) if span_azuls else head.get_text(" ", strip=True)
        hora_raw = span_azuls[1].get_text(strip=True) if len(span_azuls) > 1 else ''
        fecha_us = parse_spanish_date(fecha_raw)
        hora = normalize_time(hora_raw)

        span_negro = head.find('span', class_=lambda c: c and ('spanNegro' in (c if isinstance(c, str) else ' '.join(c))))
        sorteo_raw = span_negro.get_text(" ", strip=True) if span_negro else ''
        sorteo = re.sub(r'^\s*#', '', sorteo_raw).strip()

        tokens = [s for s in data.stripped_strings]
        nums = []
        for t in tokens:
            m = re.search(r"(\d+)", t)
            if m:
                nums.append(m.group(1))
        if len(nums) >= 2:
            numeros = f"{nums[0]}{nums[1]}"
        elif len(nums) == 1:
            numeros = nums[0]
        else:
            numeros = " ".join(tokens)

        records.append({
            'Fecha del Sorteo': fecha_us,
            'Juega': 'Loto Diaria',
            'Hora': hora,
            'Números Ganadores': numeros,
            'Sorteo': sorteo,
        })

    return records if records else None


def _parse_first_int(s):
    if pd.isna(s):
        return None
    m = re.search(r"(\d+)", str(s))
    return int(m.group(1)) if m else None


def _wilson_interval(k, n, z=1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    low = (centre - adj) / denom
    high = (centre + adj) / denom
    return (max(0.0, low), min(1.0, high))


def _analyze_boolean_series(bool_series, alpha=1, beta=1):
    s = pd.Series(bool_series).astype(int).to_numpy(dtype=int)
    N = len(s)
    successes = int(s.sum())
    p_base = successes / N if N > 0 else None
    if N >= 2:
        transitions_from_s = int(s[:-1].sum())
        s_to_s = int(((s[:-1] == 1) & (s[1:] == 1)).sum())
        p_s_given_s = (s_to_s / transitions_from_s) if transitions_from_s > 0 else None
        transitions_from_not_s = int((s[:-1] == 0).sum())
        not_s_to_s = int(((s[:-1] == 0) & (s[1:] == 1)).sum())
        p_s_given_not_s = (not_s_to_s / transitions_from_not_s) if transitions_from_not_s > 0 else None
    else:
        p_s_given_s = None
        p_s_given_not_s = None
    # current streak at the end
    streak = 0
    for x in s[::-1]:
        if x == 1:
            streak += 1
        else:
            break
    pred_bayes = (alpha + successes) / (alpha + beta + N) if N > 0 else None
    wilson_low, wilson_high = _wilson_interval(successes, N)
    return {
        'N': N,
        'successes': successes,
        'p_base': p_base,
        'p_s_given_s': p_s_given_s,
        'p_s_given_not_s': p_s_given_not_s,
        'current_streak': streak,
        'pred_bayes': pred_bayes,
        'wilson_low': wilson_low,
        'wilson_high': wilson_high,
    }


def _analyze_per_number(nums, alpha=1, beta=1, decay=0.03):
    """Calcula conteos, frecuencia, predictiva Bayesiana, recency-weighted score,
    probabilidades Markov de repetición, rachas y una heurística compuesta por
    varias medidas."""
    N = len(nums)
    counts = Counter(nums)
    # weights for recency: latest draw has age 0 -> weight 1
    weights = [math.exp(-decay * (N - 1 - i)) for i in range(N)]
    total_weight = sum(weights) if weights else 1.0
    recency_acc = {}
    for i, num in enumerate(nums):
        w = weights[i]
        recency_acc[num] = recency_acc.get(num, 0.0) + w

    # transitions for markov P(n_t == x | n_{t-1} == x)
    prevs = nums[:-1]
    curs = nums[1:]
    transitions_from = Counter(prevs)
    transitions_same = Counter()
    for a, b in zip(prevs, curs):
        if a == b:
            transitions_same[a] += 1

    # longest streaks
    longest = {}
    cur = None
    cur_len = 0
    for n in nums:
        if n == cur:
            cur_len += 1
        else:
            if cur is not None:
                longest[cur] = max(longest.get(cur, 0), cur_len)
            cur = n
            cur_len = 1
    if cur is not None:
        longest[cur] = max(longest.get(cur, 0), cur_len)

    # current streak for last number
    last = nums[-1] if nums else None
    current_streak_for_last = 0
    if last is not None:
        for x in nums[::-1]:
            if x == last:
                current_streak_for_last += 1
            else:
                break

    results = []
    for num, cnt in counts.items():
        freq = cnt / N
        recency = recency_acc.get(num, 0.0) / total_weight
        posterior_mean = (alpha + cnt) / (alpha + beta + N)
        tf = transitions_from.get(num, 0)
        ts = transitions_same.get(num, 0)
        markov_p = (ts / tf) if tf > 0 else 0.0
        cur_streak = current_streak_for_last if num == last else 0
        long_streak = longest.get(num, 0)
        # heuristic weights (tunable): recency has more weight
        score = 0.35 * recency + 0.25 * freq + 0.2 * posterior_mean + 0.1 * markov_p + 0.1 * (cur_streak / max(1, long_streak + 1))
        results.append({
            'number': int(num),
            'count': int(cnt),
            'frequency': float(freq),
            'posterior_mean': float(posterior_mean),
            'recency_score': float(recency),
            'markov_repeat_prob': float(markov_p),
            'current_streak': int(cur_streak),
            'longest_streak': int(long_streak),
            'heuristic_score': float(score),
        })

    results.sort(key=lambda x: x['heuristic_score'], reverse=True)
    for i, r in enumerate(results, start=1):
        r['rank'] = i
    return results


def _write_analysis_outputs(final_df, summary_path='loto_analysis_summary.csv', numbers_path='loto_analysis_numbers.csv', alpha=1, beta=1, decay=0.03):
    # parse numeric winning numbers
    final_df = final_df.copy()
    final_df['Sorteo'] = pd.to_numeric(final_df['Sorteo'], errors='coerce')
    final_df['__num'] = final_df['Números Ganadores'].apply(_parse_first_int)
    final_df = final_df[final_df['__num'].notna()].copy()
    final_df['__num'] = final_df['__num'].astype(int)
    # sort by sorteo (chronological ascending)
    final_df = final_df.sort_values(by='Sorteo', ascending=True).reset_index(drop=True)
    nums = final_df['__num'].tolist()
    N = len(nums)
    if N == 0:
        print('No hay números válidos para análisis.')
        return

    summary_rows = []

    # parity
    parity_res = _analyze_boolean_series([n % 2 == 0 for n in nums], alpha=alpha, beta=beta)
    for k, v in parity_res.items():
        summary_rows.append({'feature': 'parity_even', 'metric': k, 'value': v})

    # last number analysis
    last_num = nums[-1]
    last_res = _analyze_boolean_series([n == last_num for n in nums], alpha=alpha, beta=beta)
    for k, v in last_res.items():
        summary_rows.append({'feature': f'last_number_{last_num}', 'metric': k, 'value': v})

    # top 5 numbers
    counts = Counter(nums)
    top5 = [n for n, c in counts.most_common(5)]
    summary_rows.append({'feature': 'top5_list', 'metric': 'numbers', 'value': json.dumps(top5)})
    for n in top5:
        res = _analyze_boolean_series([x == n for x in nums], alpha=alpha, beta=beta)
        for k, v in res.items():
            summary_rows.append({'feature': f'top5_number_{n}', 'metric': k, 'value': v})

    # entropy of distribution (simple)
    freqs = [c / N for c in counts.values()]
    entropy = -sum((p * math.log(p) for p in freqs if p > 0))
    summary_rows.append({'feature': 'numbers', 'metric': 'unique_count', 'value': int(len(counts))})
    summary_rows.append({'feature': 'numbers', 'metric': 'entropy', 'value': float(entropy)})
    summary_rows.append({'feature': 'numbers', 'metric': 'total_draws', 'value': int(N)})

    # per-number analysis and save
    per_num = _analyze_per_number(nums, alpha=alpha, beta=beta, decay=decay)
    df_nums = pd.DataFrame(per_num)
    df_summary = pd.DataFrame(summary_rows)
    # atomic write helper
    def _atomic_write_df(dframe, path):
        tmp = str(path) + '.tmp'
        try:
            dframe.to_csv(tmp, index=False, encoding='utf-8-sig')
        except Exception:
            dframe.to_csv(tmp, index=False)
        try:
            os.replace(tmp, path)
        except Exception:
            try:
                Path(tmp).rename(path)
            except Exception:
                pass

    try:
        _atomic_write_df(df_nums, numbers_path)
        _atomic_write_df(df_summary, summary_path)
    except Exception as e:
        print('Advertencia al guardar análisis:', e)

    logging.info("Análisis guardado en '%s' y '%s' (%d números analizados)", summary_path, numbers_path, len(df_nums))
    print(f"Análisis guardado en '{summary_path}' y '{numbers_path}' ({len(df_nums)} números analizados)")


### ----------------- Predicciones y scoring ----------------- ###


def _prepare_numeric_df(df):
    df2 = df.copy()
    df2['Sorteo'] = pd.to_numeric(df2['Sorteo'], errors='coerce')
    df2['__num'] = df2['Números Ganadores'].apply(_parse_first_int)
    df2 = df2[df2['__num'].notna()].copy()
    df2['__num'] = df2['__num'].astype(int)
    df2 = df2.sort_values(by='Sorteo', ascending=True).reset_index(drop=True)
    return df2


def _load_predictions(path='predictions.csv'):
    p = Path(path)
    if not p.exists():
        cols = ['predicted_for_sorteo','method','predicted_numbers','predicted_group','predicted_at','evaluated','evaluated_at','actual_sorteo','actual_number','hit_number','hit_group','hit_rank']
        return pd.DataFrame(columns=cols)
    try:
        return pd.read_csv(p, dtype=str)
    except Exception:
        return pd.read_csv(p, dtype=str, encoding='latin1')


def _save_predictions(df, path='predictions.csv'):
    tmp = str(path) + '.tmp'
    try:
        df.to_csv(tmp, index=False, encoding='utf-8-sig')
    except Exception:
        df.to_csv(tmp, index=False)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).rename(path)
        except Exception:
            pass


def _apply_financial_metrics(scores):
    if scores is None or len(scores) == 0:
        return scores

    for col in ['total_predictions', 'number_hits', 'group_hits', 'rank_sum', 'rank_hits_count']:
        if col not in scores.columns:
            scores[col] = 0

    for col in ['total_predictions', 'number_hits', 'group_hits', 'rank_sum', 'rank_hits_count']:
        scores[col] = pd.to_numeric(scores[col], errors='coerce').fillna(0)

    scores['total_invertido'] = scores['total_predictions'].astype(int) * PREDICTION_NUMBERS_PER_ROW * COST_PER_NUMBER
    scores['rendimiento'] = scores['number_hits'].astype(int) * PAYOUT_PER_HIT
    scores['total'] = scores['rendimiento'] - scores['total_invertido']
    scores['roi'] = scores.apply(
        lambda row: (row['total'] / row['total_invertido']) if row['total_invertido'] else 0.0,
        axis=1,
    )
    return scores


def _load_scores(path='predictions_scores.csv'):
    p = Path(path)
    if not p.exists():
        cols = ['method','total_predictions','number_hits','group_hits','rank_sum','rank_hits_count','total_invertido','rendimiento','total','roi','last_update']
        return pd.DataFrame(columns=cols)
    try:
        scores = pd.read_csv(p, dtype=str)
    except Exception:
        scores = pd.read_csv(p, dtype=str, encoding='latin1')
    return _apply_financial_metrics(scores)


def _save_scores(df, path='predictions_scores.csv'):
    tmp = str(path) + '.tmp'
    try:
        df.to_csv(tmp, index=False, encoding='utf-8-sig')
    except Exception:
        df.to_csv(tmp, index=False)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).rename(path)
        except Exception:
            pass


def _update_scores_from_evaluated(preds_df, scores_path='predictions_scores.csv'):
    # preds_df: DataFrame containing only the rows that were just evaluated (or all evaluated rows)
    if preds_df is None or len(preds_df) == 0:
        return
    scores = _load_scores(scores_path)
    # ensure numeric columns
    for col in ['total_predictions','number_hits','group_hits','rank_sum','rank_hits_count']:
        if col not in scores.columns:
            scores[col] = 0
    for _, row in preds_df.iterrows():
        method = str(row.get('method','')).strip()
        if method == '':
            continue
        try:
            hit_number = str(row.get('hit_number','')).lower() in ('1','true','yes')
        except Exception:
            hit_number = False
        try:
            hit_group = str(row.get('hit_group','')).lower() in ('1','true','yes')
        except Exception:
            hit_group = False
        rank = None
        try:
            rank = int(row.get('hit_rank')) if (row.get('hit_rank') not in (None, '', 'nan')) else None
        except Exception:
            rank = None

        if method in scores['method'].values:
            idx = scores.index[scores['method']==method][0]
        else:
            idx = len(scores)
            scores.loc[idx, 'method'] = method
            scores.loc[idx, 'total_predictions'] = 0
            scores.loc[idx, 'number_hits'] = 0
            scores.loc[idx, 'group_hits'] = 0
            scores.loc[idx, 'rank_sum'] = 0
            scores.loc[idx, 'rank_hits_count'] = 0

        scores.at[idx, 'total_predictions'] = int(scores.at[idx, 'total_predictions']) + 1
        if hit_number:
            scores.at[idx, 'number_hits'] = int(scores.at[idx, 'number_hits']) + 1
        if hit_group:
            scores.at[idx, 'group_hits'] = int(scores.at[idx, 'group_hits']) + 1
        if rank is not None:
            scores.at[idx, 'rank_sum'] = int(scores.at[idx, 'rank_sum']) + int(rank)
            scores.at[idx, 'rank_hits_count'] = int(scores.at[idx, 'rank_hits_count']) + 1

        scores.at[idx, 'last_update'] = datetime.utcnow().isoformat()

    scores = _apply_financial_metrics(scores)
    _save_scores(scores, scores_path)
    logging.info('Scores actualizados: %s', scores_path)


def _evaluate_pending_predictions(final_df, predictions_path='predictions.csv', scores_path='predictions_scores.csv'):
    final_num_df = _prepare_numeric_df(final_df)
    mapping = dict(zip(final_num_df['Sorteo'].astype(int).tolist(), final_num_df['__num'].tolist()))
    preds = _load_predictions(predictions_path)
    if preds is None or preds.empty:
        return
    # normalize evaluated flag
    preds['evaluated'] = preds['evaluated'].fillna('').astype(str)
    eval_mask = preds['evaluated'].str.lower().isin(['true','1','yes'])
    pending_idx = preds.index[~eval_mask]
    if len(pending_idx) == 0:
        return

    evaluated_rows = []
    for i in list(pending_idx):
        try:
            target = int(float(preds.at[i, 'predicted_for_sorteo']))
        except Exception:
            continue
        if target not in mapping:
            # still not drawn
            continue
        actual_num = int(mapping[target])
        actual_group = 0 if actual_num < 50 else 1

        # load predicted numbers
        pred_list = []
        try:
            pred_list = json.loads(preds.at[i, 'predicted_numbers']) if preds.at[i, 'predicted_numbers'] else []
        except Exception:
            # maybe stored as Python list string
            try:
                pred_list = eval(preds.at[i, 'predicted_numbers'])
            except Exception:
                pred_list = []
        pred_list = [int(x) for x in pred_list if x is not None]

        hit_number = actual_num in pred_list
        hit_rank = (pred_list.index(actual_num) + 1) if hit_number else None

        # predicted group: try column, else majority of predicted numbers
        predicted_group = None
        try:
            if 'predicted_group' in preds.columns and preds.at[i, 'predicted_group'] not in (None, '', 'nan'):
                predicted_group = int(float(preds.at[i, 'predicted_group']))
        except Exception:
            predicted_group = None
        if predicted_group is None and len(pred_list) > 0:
            grp_count = sum(1 for x in pred_list if int(x) < 50)
            predicted_group = 0 if grp_count >= (len(pred_list)/2) else 1

        hit_group = (predicted_group == actual_group)

        preds.at[i, 'actual_sorteo'] = int(target)
        preds.at[i, 'actual_number'] = int(actual_num)
        preds.at[i, 'hit_number'] = bool(hit_number)
        preds.at[i, 'hit_rank'] = int(hit_rank) if hit_rank is not None else ''
        preds.at[i, 'hit_group'] = bool(hit_group)
        preds.at[i, 'evaluated'] = True
        preds.at[i, 'evaluated_at'] = datetime.utcnow().isoformat()

        evaluated_rows.append({
            'method': preds.at[i, 'method'],
            'hit_number': hit_number,
            'hit_group': hit_group,
            'hit_rank': hit_rank
        })

    if evaluated_rows:
        # update scores
        evaluated_df = pd.DataFrame(evaluated_rows)
        _update_scores_from_evaluated(evaluated_df, scores_path)
        # save updated predictions
        _save_predictions(preds, predictions_path)
        logging.info('Evaluadas %d predicciones y actualizado score.', len(evaluated_rows))
        print(f"Evaluadas {len(evaluated_rows)} predicciones y actualizado score.")
        # return details
        return len(evaluated_rows), evaluated_rows
    return 0, []


def _generate_predictions(final_df, predictions_path='predictions.csv', scores_path='predictions_scores.csv', n_numbers=10):
    dfnum = _prepare_numeric_df(final_df)
    if dfnum.empty:
        return 0, []
    latest_sorteo = int(dfnum['Sorteo'].max())
    target = latest_sorteo + 1

    preds = _load_predictions(predictions_path)
    # if there are pending predictions for this target (not evaluated), do not create new ones
    if not preds.empty:
        mask_target = preds['predicted_for_sorteo'].astype(str) == str(target)
        if mask_target.any():
            # if any for this target are not evaluated, skip
            sub = preds[mask_target]
            sub_eval = sub['evaluated'].astype(str).str.lower().isin(['true','1','yes'])
            if not sub_eval.all():
                print(f"Predicciones pendientes para el sorteo {target}; no se generan nuevas predicciones.")
                return 0, []
            # else: predictions for this target already exist and are evaluated; do not regenerate
            print(f"Predicciones para el sorteo {target} ya existen (evaluadas); no se generan nuevas.")
            return 0, []

    nums = dfnum['__num'].tolist()
    per_num = _analyze_per_number(nums, alpha=1, beta=1, decay=0.03)
    counts = dict(Counter(nums))

    # helper to fill up to n_numbers from available candidates
    def fill_list(src_list):
        out = []
        for v in src_list:
            if len(out) >= n_numbers:
                break
            if v not in out:
                out.append(v)
        # fill with numbers not in out
        if len(out) < n_numbers:
            for cand in range(0, 100):
                if cand not in out:
                    out.append(cand)
                if len(out) >= n_numbers:
                    break
        return out

    # prepare candidate lists
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
    # anti-freq (least frequent)
    all_numbers = list(range(0,100))
    counts_full = {i: counts.get(i, 0) for i in all_numbers}
    anti_list = sorted(all_numbers, key=lambda x: (counts_full[x], x))

    # --- Nuevo: candidatos por rachas de grupo y por buckets automáticos ---
    N_hist = len(nums)
    # número de buckets automático (heurística basada en sqrt de la cantidad de históricos)
    k_auto = max(2, min(10, int(round(math.sqrt(max(1, N_hist))))))
    k_fine = max(2, min(20, int(round(k_auto * 2))))

    # mapa rápido number -> per_num entry
    per_map = {p['number']: p for p in per_num}

    def _bucketize(k):
        bucket_size = int(math.ceil(100.0 / k))
        buckets = []
        for i in range(k):
            lo = i * bucket_size
            hi = min(99, (i + 1) * bucket_size - 1)
            bucket_nums = list(range(lo, hi + 1))
            bucket_count = sum(1 for v in nums if lo <= v <= hi)
            bucket_recency = sum(per_map.get(n, {}).get('recency_score', 0.0) for n in bucket_nums)
            transitions_from = 0
            transitions_same = 0
            for a, b in zip(nums[:-1], nums[1:]):
                if lo <= a <= hi:
                    transitions_from += 1
                    if lo <= b <= hi:
                        transitions_same += 1
            markov_bucket = (transitions_same / transitions_from) if transitions_from > 0 else 0.0
            bucket_score = 0.55 * bucket_recency + 0.35 * (bucket_count / max(1, N_hist)) + 0.10 * markov_bucket
            buckets.append({'i': i, 'lo': lo, 'hi': hi, 'nums': bucket_nums, 'count': bucket_count, 'recency': bucket_recency, 'markov': markov_bucket, 'score': bucket_score})
        buckets = sorted(buckets, key=lambda x: x['score'], reverse=True)
        return buckets

    buckets_auto = _bucketize(k_auto)
    buckets_fine = _bucketize(k_fine)

    # heurística de rachas por grupo (0: 0-49, 1: 50-99)
    group0_stats = _analyze_boolean_series([v < 50 for v in nums])
    group1_stats = _analyze_boolean_series([v >= 50 for v in nums])
    if group0_stats.get('current_streak', 0) >= 2:
        favored_group = 0
    elif group1_stats.get('current_streak', 0) >= 2:
        favored_group = 1
    else:
        favored_group = 0 if group0_stats.get('pred_bayes', 0.5) >= 0.5 else 1

    # candidatos por grupo (ordenados por heuristic_score dentro del grupo)
    group_candidates = [int(x['number']) for x in by_heur if (x['number'] < 50) == (favored_group == 0)]
    group_candidates = group_candidates[:n_numbers]

    def _candidates_from_buckets(buckets_list):
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

    buckets_auto_list = _candidates_from_buckets(buckets_auto)
    buckets_fine_list = _candidates_from_buckets(buckets_fine)
    # --- Nuevo: bucket-first allocation (distribuye los n_numbers entre buckets) ---
    def _allocate_and_select(buckets_list, n_numbers=10, min_per_bucket=0):
        k = len(buckets_list)
        if k == 0:
            return []
        # weights from bucket score (guardando no-negatividad)
        weights = [max(0.0, float(b.get('score', 0.0))) for b in buckets_list]
        totalw = sum(weights)
        if totalw <= 0:
            weights = [1.0] * k
            totalw = float(k)

        ideals = [w / totalw * n_numbers for w in weights]
        alloc = [int(math.floor(x)) for x in ideals]
        remaining = n_numbers - sum(alloc)
        # repartir remanentes por mayor parte fraccionaria
        fracs = sorted([(ideals[i] - alloc[i], i) for i in range(k)], reverse=True)
        idx = 0
        while remaining > 0 and idx < len(fracs):
            alloc[fracs[idx][1]] += 1
            remaining -= 1
            idx += 1

        # aplicar mínimo por bucket si solicitado
        if min_per_bucket and k <= n_numbers:
            for i in range(k):
                if alloc[i] < min_per_bucket:
                    alloc[i] = min_per_bucket
            # ajustar si excede
            total_alloc = sum(alloc)
            if total_alloc > n_numbers:
                to_reduce = total_alloc - n_numbers
                # reducir desde buckets con más asignado que el mínimo
                while to_reduce > 0:
                    candidates = [i for i in range(k) if alloc[i] > min_per_bucket]
                    if not candidates:
                        break
                    # elegir el bucket con mayor allocation
                    j = max(candidates, key=lambda x: alloc[x])
                    alloc[j] -= 1
                    to_reduce -= 1

        # seleccionar números dentro de cada bucket por heuristic_score
        chosen = []
        per_map_local = {p['number']: p for p in per_num}
        for i, b in enumerate(buckets_list):
            want = alloc[i]
            if want <= 0:
                continue
            bucket_nums = b.get('nums', [])
            # ordenar por heuristic_score dentro del bucket
            bucket_sorted = sorted(bucket_nums, key=lambda n: per_map_local.get(int(n), {}).get('heuristic_score', 0.0), reverse=True)
            picked = []
            for n in bucket_sorted:
                if len(picked) >= want:
                    break
                if n not in chosen:
                    picked.append(int(n))
            # si faltan, completar con números no usados en el bucket
            for n in bucket_nums:
                if len(picked) >= want:
                    break
                if n not in chosen and n not in picked:
                    picked.append(int(n))
            chosen.extend(picked)

        # rellenar si es necesario con top heuristics
        if len(chosen) < n_numbers:
            fallback = [int(p['number']) for p in sorted(per_num, key=lambda x: x['heuristic_score'], reverse=True)]
            for n in fallback:
                if len(chosen) >= n_numbers:
                    break
                if n not in chosen:
                    chosen.append(n)
        # final
        return chosen[:n_numbers]

    # obtener buckets para K=2 (grupos) y auto
    buckets_k2 = _bucketize(2)
    bucket_first_k2 = _allocate_and_select(buckets_k2, n_numbers=n_numbers, min_per_bucket=1 if len(buckets_k2) <= n_numbers else 0)
    bucket_first_auto = _allocate_and_select(buckets_auto, n_numbers=n_numbers, min_per_bucket=1 if len(buckets_auto) <= n_numbers else 0)
    
    # helper to fill lists of arbitrary length (k)
    def fill_list_n(src_list, k):
        out = []
        for v in src_list:
            if len(out) >= k:
                break
            if v not in out:
                out.append(int(v))
        if len(out) < k:
            for cand in range(0, 100):
                if cand not in out:
                    out.append(cand)
                if len(out) >= k:
                    break
        return out

    # prepare 20-number variants
    bucket_first_k2_20 = _allocate_and_select(buckets_k2, n_numbers=20, min_per_bucket=1 if len(buckets_k2) <= 20 else 0)
    bucket_first_auto_20 = _allocate_and_select(buckets_auto, n_numbers=20, min_per_bucket=1 if len(buckets_auto) <= 20 else 0)
    group_candidates_20 = [int(x['number']) for x in by_heur if (x['number'] < 50) == (favored_group == 0)][:20]

    # --- Interval-based methods (new group) ---
    def _interval_buckets(k):
        buckets = []
        num_buckets = int(math.ceil(100.0 / k))
        for i in range(num_buckets):
            lo = i * k
            hi = min(99, (i + 1) * k - 1)
            bucket_nums = list(range(lo, hi + 1))
            bucket_count = sum(1 for v in nums if lo <= v <= hi)
            bucket_recency = sum(per_map.get(n, {}).get('recency_score', 0.0) for n in bucket_nums)
            transitions_from = 0
            transitions_same = 0
            for a, b in zip(nums[:-1], nums[1:]):
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
        top_intervals = 1 if k >= 10 else 1
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
            return fill_list_n([], want)
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

    # Mantener métodos basados en buckets y rachas de grupo,
    # incluyendo las variantes de 20 números y los nuevos métodos por intervalo.
    methods = {
        'bucket_first_k2': fill_list(bucket_first_k2),
        'bucket_first_auto': fill_list(bucket_first_auto),
        'group_streaks': fill_list(group_candidates),
        'bucket_first_k2_20': fill_list_n(bucket_first_k2_20, 20),
        'bucket_first_auto_20': fill_list_n(bucket_first_auto_20, 20),
        'group_streaks_20': fill_list_n(group_candidates_20, 20),
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

    # No se generan métodos aleatorios ni los antiguos.

    # write to predictions CSV (one row per method)
    rows = []
    now = datetime.utcnow().isoformat()
    for mname, plist in methods.items():
        if not plist:
            continue
        # predicted group by majority
        grp_count = sum(1 for x in plist if int(x) < 50)
        predicted_group = 0 if grp_count >= (len(plist)/2) else 1
        rows.append({
            'predicted_for_sorteo': int(target),
            'method': mname,
            'predicted_numbers': json.dumps(plist),
            'predicted_group': int(predicted_group),
            'predicted_at': now,
            'evaluated': False,
            'evaluated_at': '',
            'actual_sorteo': '',
            'actual_number': '',
            'hit_number': '',
            'hit_group': '',
            'hit_rank': ''
        })

    if rows:
        preds_df = _load_predictions(predictions_path)
        new_df = pd.DataFrame(rows)
        combined = pd.concat([preds_df, new_df], ignore_index=True, sort=False)
        _save_predictions(combined, predictions_path)
        logging.info('Generadas %d predicciones para sorteo %s (archivo: %s)', len(rows), target, predictions_path)
        print(f"Generadas {len(rows)} predicciones para sorteo {target} (archivo: {predictions_path})")
        return len(rows), [r['method'] for r in rows]
    return 0, []



def main():
    parser = argparse.ArgumentParser(description="Extrae filas 'Loto Diaria' desde la web")
    parser.add_argument('--csv', default='loto_diaria.csv', help='Archivo CSV de salida')
    parser.add_argument('--excel', default='loto_diaria.xlsx', help='Archivo Excel de salida')
    parser.add_argument('--url', default='https://www.yelu.com.ni/lottery/results/history', help='URL para buscar la tabla')
    parser.add_argument('--no-lock', action='store_true', help='No usar lockfile (útil para tests)')
    parser.add_argument('--lock-file', default='extract_loto_diaria.lock', help='Ruta del lockfile')
    parser.add_argument('--lock-timeout', type=int, default=3600, help='Segundos para considerar lock stale')
    parser.add_argument('--log-file', default=None, help='Archivo de log (si se desea sobreescribir)')
    args = parser.parse_args()

    # optional logging reconfiguration
    if args.log_file:
        try:
            fh = logging.FileHandler(args.log_file)
            fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
            fh.setLevel(logging.INFO)
            logging.getLogger().addHandler(fh)
            logging.info('Logging adicional a %s', args.log_file)
        except Exception:
            pass

    # acquire lock (if enabled)
    lock_acquired = True
    if not args.no_lock:
        # register termination handlers
        signal.signal(signal.SIGINT, _on_terminate)
        signal.signal(signal.SIGTERM, _on_terminate)
        lock_acquired = acquire_lock(args.lock_file, timeout=args.lock_timeout)
        if not lock_acquired:
            msg = f"No se pudo adquirir lock {args.lock_file}; posiblemente ya se está ejecutando. Saliendo."
            print(msg)
            logging.info(msg)
            return

    # 1) Usar solo el scraping directo en https://loto.com.ni/diaria/
    records = None
    print('Usando scraping directo en https://loto.com.ni/diaria/')
    try:
        records = fetch_records_from_loto_site('https://loto.com.ni/diaria/')
    except Exception as e:
        logger.warning('Error en scraping web: %s', e)
        records = None

    if records:
        print('Scraping web encontró %d registros en https://loto.com.ni/diaria/' % len(records))
        # mostrar últimos sorteos (verificación rápida)
        try:
            fr_df = pd.DataFrame(records)
            if 'Sorteo' in fr_df.columns:
                fr_df['_sorteo_int'] = fr_df['Sorteo'].apply(_parse_first_int)
                fr_df_sorted = fr_df.sort_values(by='_sorteo_int', ascending=False)
                show = fr_df_sorted.head(10)
                print("Últimos sorteos encontrados (Sorteo | Fecha | Hora | Números Ganadores):")
                for _, r in show.iterrows():
                    print(f"  {r.get('Sorteo','')} | {r.get('Fecha del Sorteo','')} | {r.get('Hora','')} | {r.get('Números Ganadores','')}")
            else:
                print('Últimos registros (muestra):')
                for r in records[:10]:
                    print('  ' + ', '.join(f"{k}:{v}" for k, v in r.items()))
        except Exception as e:
            logger.warning('No se pudieron imprimir últimos sorteos del scraping web: %s', e)
    else:
        print('El scraping web no devolvió datos.')

    if not records:
        print("No se encontraron filas con 'Loto Diaria'.")
        return

    df = pd.DataFrame(records)

    # 2) Combinar con CSV existente (si existe) y remover duplicados por 'Sorteo'
    csv_path = Path(args.csv)
    if csv_path.exists():
        try:
            try:
                existing = pd.read_csv(csv_path, encoding='utf-8-sig', dtype=str)
            except Exception:
                existing = pd.read_csv(csv_path, dtype=str, encoding='latin1')

            # detectar nuevos sorteos por número (analizamos dígitos dentro de la columna 'Sorteo')
            def _ids_from_series(ser):
                ids = set()
                for v in ser.astype(str).tolist():
                    n = _parse_first_int(v)
                    if n is not None:
                        ids.add(int(n))
                return ids

            existing_ids = _ids_from_series(existing.get('Sorteo', pd.Series(dtype=str)))
            df_ids = _ids_from_series(df.get('Sorteo', pd.Series(dtype=str)))
            new_ids = df_ids - existing_ids
            if not new_ids:
                print("Se encontraron datos pero no hay nuevos sorteo(s) según 'Sorteo'.")
                logging.info('Datos encontrados pero no nuevos sorteos segun Sorteo')
            else:
                print(f"Detectados {len(new_ids)} nuevos sorteo(s): {sorted(list(new_ids))[:10]}")
                logging.info('Nuevos sorteos detectados: %s', new_ids)

            combined = pd.concat([existing, df], ignore_index=True)
            combined.drop_duplicates(subset=['Sorteo'], keep='last', inplace=True)
            final_df = combined
        except Exception as e:
            print(f"Advertencia: no se pudo leer {args.csv}: {e}. Guardando solo nuevos registros.")
            logging.exception('Error leyendo CSV existente')
            df.drop_duplicates(subset=['Sorteo'], keep='last', inplace=True)
            final_df = df
    else:
        df.drop_duplicates(subset=['Sorteo'], keep='last', inplace=True)
        final_df = df

    # 3) Sobrescribir CSV y guardar Excel
    final_df.to_csv(args.csv, index=False, encoding='utf-8-sig')
    print(f"Guardadas {len(final_df)} filas en {args.csv}")

    # Ejecutar análisis probabilístico intensivo y guardar resultados separados
    try:
        _write_analysis_outputs(final_df, summary_path='loto_analysis_summary.csv', numbers_path='loto_analysis_numbers.csv', alpha=1, beta=1, decay=0.03)
    except Exception as e:
        logging.exception('El análisis no pudo ejecutarse')
        print(f"Advertencia: el análisis no pudo ejecutarse: {e}")

    # Evaluar predicciones pendientes (si apareció un nuevo sorteo) y generar predicciones para el siguiente
    evaluated_count = 0
    evaluated_details = []
    generated_count = 0
    generated_methods = []
    try:
        evaluated_count, evaluated_details = _evaluate_pending_predictions(final_df, predictions_path='predictions.csv', scores_path='predictions_scores.csv')
    except Exception as e:
        logging.exception('No se pudo evaluar predicciones pendientes')
        print(f"Advertencia: no se pudo evaluar predicciones pendientes: {e}")
    try:
        generated_count, generated_methods = _generate_predictions(final_df, predictions_path='predictions.csv', scores_path='predictions_scores.csv', n_numbers=10)
    except Exception as e:
        logging.exception('No se pudo generar nuevas predicciones')
        print(f"Advertencia: no se pudo generar nuevas predicciones: {e}")

    # summary
    if evaluated_count:
        print(f"Resumen: evaluadas {evaluated_count} predicciones.")
        logging.info('Resumen: evaluadas %d predicciones', evaluated_count)
    if generated_count:
        print(f"Resumen: generadas {generated_count} predicciones (métodos: {', '.join(generated_methods)})")
        logging.info('Resumen: generadas %d predicciones: %s', generated_count, generated_methods)

    # release lock
    if not args.no_lock and lock_acquired:
        try:
            release_lock(args.lock_file)
        except Exception:
            pass


if __name__ == '__main__':
    main()
