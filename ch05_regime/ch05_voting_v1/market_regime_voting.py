"""Chapter 5: 11 data-driven analysts; net voting only, no continuous score.
Python 3.10+, standard library only. See README.md for assumptions.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import re
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
TW = timezone(timedelta(hours=8))
DEFAULTS = {
    'pcr_low': 0.7, 'pcr_high': 1.2,
    'copper_gold_band': 0.02, 'trend_band': 0.01,
    'ewt_daily_band': 0.005, 'vix_term_band': 0.02,
    'max_age_days': 7, 'breadth_calendar_days': 120,
}
SYMBOLS = {'taiex': '^TWII', 'vix': '^VIX', 'vix3m': '^VIX3M',
           'copper': 'HG=F', 'gold': 'GC=F', 'sp500': '^GSPC',
           'sox': '^SOX', 'ewt': 'EWT', 'dxy': 'DX-Y.NYB'}


def number(x):
    v = float(str(x).replace(',', '').replace('%', '').strip())
    if not math.isfinite(v):
        raise ValueError('non-finite number')
    return v


def parse_date(s):
    s = str(s).strip().replace('/', '-')
    if re.fullmatch(r'\d{8}', s):
        s = f'{s[:4]}-{s[4:6]}-{s[6:]}'
    parts = s.split('-')
    if len(parts) == 3 and int(parts[0]) < 1911:
        parts[0] = str(int(parts[0]) + 1911)
    return date(*map(int, parts))


def normalize(rows, asof):
    result = {}
    for d, v in rows:
        d = parse_date(d) if not isinstance(d, date) else d
        v = number(v)
        if d <= asof:
            if d in result and result[d] != v:
                raise ValueError(f'conflicting duplicate date: {d}')
            result[d] = v
    if not result:
        raise ValueError('no observations on/before asof')
    return sorted(result.items())


def request(url, cache, permanent=False):
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.txt')
    if path.exists() and (permanent or time.time() - path.stat().st_mtime < 21600):
        return path.read_text(encoding='utf-8')
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json,text/csv,*/*'})
    ctx = ssl.create_default_context()
    for attempt in range(3):
        try:
            with urlopen(req, timeout=60, context=ctx) as resp:
                body = resp.read().decode('utf-8-sig')
            path.write_text(body, encoding='utf-8')
            return body
        except (TimeoutError, URLError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                # Narrow compatibility fallback for this precise TWSE error.
                # Keep trust-chain verification and hostname checks enabled.
                if (urlsplit(url).hostname in ('www.twse.com.tw', 'wwwc.twse.com.tw')
                        and 'Missing Subject Key Identifier' in str(reason)
                        and ctx.verify_flags & ssl.VERIFY_X509_STRICT):
                    print('  TWSE TLS compatibility retry (certificate and hostname checks remain enabled)', flush=True)
                    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
                    continue
                raise
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 2:
                raise
            print(f'  Network retry {attempt + 1}/2: {type(exc).__name__}', flush=True)
            time.sleep(2 ** attempt)
    raise RuntimeError('request retries exhausted')


def yahoo(symbol, asof, cache):
    # End at UTC midnight after asof. Filter again using exchange-local dates.
    end = int(datetime.combine(asof + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    start = end - 400 * 86400
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe="")}'
           f'?period1={start}&period2={end}&interval=1d')
    obj = json.loads(request(url, cache))
    chart = obj['chart']['result'][0]
    offset = timedelta(seconds=chart['meta'].get('gmtoffset', 0))
    rows = []
    for t, c in zip(chart['timestamp'], chart['indicators']['quote'][0]['close']):
        if c is not None:
            d = (datetime.fromtimestamp(t, timezone.utc) + offset).date()
            rows.append((d, c))
    return normalize(rows, asof)


def fred(asof, cache):
    url = ('https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y'
           f'&cosd={asof-timedelta(days=30)}&coed={asof}')
    reader = csv.reader(io.StringIO(request(url, cache)))
    next(reader)
    return normalize([(d, v) for d, v in reader if v not in ('', '.')], asof)


def pcr(asof, cache):
    url = 'https://openapi.taifex.com.tw/v1/PutCallRatio'
    rows = json.loads(request(url, cache))
    # Official English JSON field names; fail visibly if schema changes.
    return normalize([(r['Date'], number(r['PutCallVolumeRatio%']) / 100) for r in rows], asof)


def parse_breadth(obj):
    tables = obj.get('tables', [])
    # Older TWSE response schema.
    if not tables:
        tables = [{'fields': obj[k], 'data': obj.get(k.replace('fields', 'data'), [])}
                  for k in obj if k.startswith('fields')]
    for t in tables:
        fields = [re.sub('<[^>]*>', '', str(f)).strip() for f in t.get('fields', [])]
        if '股票' not in fields:
            continue
        col = fields.index('股票')
        found = {}
        for row in t.get('data', []):
            label = re.sub('<[^>]*>', '', str(row[0]))
            for key, word in [('advances', '上漲'), ('declines', '下跌')]:
                if word in label:
                    # 277(12) means 277 advances, including 12 limit-up stocks.
                    found[key] = int(re.search(r'\d[\d,]*', str(row[col])).group().replace(',', ''))
        if len(found) == 2:
            return found
    raise ValueError('TWSE stock advances/declines table missing')


def breadth(asof, cache, days):
    rows, failures = [], 0
    for i in range(days):
        d = asof - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        url = f'https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={d:%Y%m%d}&type=MS'
        try:
            obj = json.loads(request(url, cache, permanent=True))
            if obj.get('stat') != 'OK':
                continue  # holiday / no published data
            if obj.get('date') and parse_date(obj['date']) != d:
                raise ValueError('TWSE returned a different date')
            r = parse_breadth(obj)
            rows.append((d, r['advances'], r['declines']))
            failures = 0
        except Exception as e:
            failures += 1
            print(f'  breadth {d}: {type(e).__name__}: {e}', flush=True)
            if failures >= 3:
                if rows:
                    break
                raise ValueError('three consecutive breadth fetch failures; need 60+ rows') from e
        finally:
            time.sleep(1)  # polite rate limit, successful historical responses cached
    if not rows:
        raise ValueError('no breadth observations')
    return sorted(rows)


def read_local(path, asof, is_breadth=False):
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if not is_breadth:
        return normalize([(r['date'], r['close'] if 'close' in r else r['value']) for r in rows], asof)
    out = {}
    for r in rows:
        d = parse_date(r['date'])
        if d <= asof:
            a, b = number(r['advances']), number(r['declines'])
            if min(a, b) < 0 or a + b <= 0:
                raise ValueError('invalid breadth counts')
            if d in out:
                raise ValueError('duplicate breadth date')
            out[d] = (a, b)
    return [(d, *out[d]) for d in sorted(out)]


def align(a, b):
    x, y = dict(a), dict(b)
    return [(d, x[d], y[d]) for d in sorted(x.keys() & y.keys())]


def classify(bull, bear):
    if min(bull, bear) < 0:
        raise ValueError('votes must be nonnegative')
    net = bull - bear
    if net >= 3:
        return 'BULL'
    if net <= -3:
        return 'BEAR'
    if net == -2:
        return 'RISK_OFF'
    return 'NEUTRAL'


def ema(values, span):
    value = values[0]
    alpha = 2 / (span + 1)
    for v in values[1:]:
        value += alpha * (v - value)
    return value


def evaluate(data, asof, cfg):
    votes = []

    def need(key, n=1, positive=True):
        rows = data.get(key, [])
        if len(rows) < n:
            raise ValueError(f'{key}: need {n}, got {len(rows)} rows')
        if (asof - rows[-1][0]).days > cfg['max_age_days']:
            raise ValueError(f'{key}: stale, last={rows[-1][0]}')
        if any(d > asof for d, *_ in rows):
            raise ValueError(f'{key}: future data')
        if positive and any(v <= 0 for _, v in rows):
            raise ValueError(f'{key}: nonpositive price/ratio')
        return rows

    def add(name, fn):
        try:
            bull, bear, evidence = fn()
            votes.append(dict(analyst=name, status='OK', bull=bull, bear=bear, evidence=evidence))
        except Exception as e:
            votes.append(dict(analyst=name, status='MISSING', bull=0, bear=0, evidence=str(e)))

    def index_vote():
        r = need('taiex', 60); values = [v for _, v in r]
        last, ma20, ma60 = values[-1], sum(values[-20:])/20, sum(values[-60:])/60
        # Independent, cumulative rules. Below means level, not crossing event.
        bull = int(last > ma60) + int(ma20 > ma60)
        bear = int(last < ma20) + 2 * int(last < ma60 * .95)
        return bull, bear, dict(date=str(r[-1][0]), close=last, ma20=ma20, ma60=ma60,
                               rules=['close>MA60:+1', 'MA20>MA60:+1', 'close<MA20:-1', 'close<0.95*MA60:-2'])

    def vix_vote():
        r = need('vix'); v = r[-1][1]
        return int(v < 16), 2 if v > 28 else int(v >= 20), dict(date=str(r[-1][0]), vix=v)

    def pcr_vote():
        r = need('pcr'); v = r[-1][1]
        return int(v >= cfg['pcr_high']), int(v <= cfg['pcr_low']), dict(date=str(r[-1][0]), volume_ratio=v)

    def cg_vote():
        rows = align(need('copper', 60), need('gold', 60))
        ratios = [(d, c/g) for d, c, g in rows]
        if len(ratios) < 60: raise ValueError('copper/gold: need 60 common dates')
        if (asof-ratios[-1][0]).days > cfg['max_age_days']: raise ValueError('stale copper/gold common date')
        avg = sum(v for _, v in ratios[-60:])/60
        relative = ratios[-1][1]/avg - 1
        return int(relative > cfg['copper_gold_band']), int(relative < -cfg['copper_gold_band']), dict(date=str(rows[-1][0]), ratio_relative_ma60=relative)

    def yield_vote():
        r = need('yield_spread', positive=False); v = r[-1][1]
        return int(v > 0), int(v < 0), dict(date=str(r[-1][0]), ten_year_minus_two_year_pct_points=v)

    def term_vote():
        rows = align(need('vix'), need('vix3m'))
        if not rows: raise ValueError('no common VIX dates')
        d, v1, v3 = rows[-1]
        if (asof-d).days > cfg['max_age_days']: raise ValueError('stale common VIX date')
        relative = v3/v1 - 1
        return int(relative > cfg['vix_term_band']), int(relative < -cfg['vix_term_band']), dict(date=str(d), proxy='VIX3M/VIX, NOT futures curve', ratio=v3/v1)

    def breadth_vote():
        r = need('breadth', 60, positive=False)
        # Ratio-adjusted McClellan; exclude unchanged/untraded stocks.
        vals = [1000*(a-b)/(a+b) for _, a, b in r]
        osc = ema(vals, 19) - ema(vals, 39)
        return int(osc > 0), int(osc < 0), dict(date=str(r[-1][0]), advances=r[-1][1], declines=r[-1][2], mcclellan=osc, universe='TWSE stock column; excludes TPEX')

    def trend_vote(key, reverse=False):
        r = need(key, 20); ma = sum(v for _, v in r[-20:])/20
        rel = r[-1][1]/ma - 1
        signal = -rel if reverse else rel
        return int(signal > cfg['trend_band']), int(signal < -cfg['trend_band']), dict(date=str(r[-1][0]), relative_ma20=rel)

    def ewt_vote():
        r = need('ewt', 2); ret = r[-1][1]/r[-2][1]-1
        return int(ret > cfg['ewt_daily_band']), int(ret < -cfg['ewt_daily_band']), dict(date=str(r[-1][0]), daily_return=ret, proxy='US-listed EWT regular-session close; NOT after-hours')

    for name, fn in [('01_TAIEX', index_vote), ('02_VIX', vix_vote), ('03_PCR', pcr_vote),
                     ('04_COPPER_GOLD', cg_vote), ('05_YIELD_CURVE', yield_vote),
                     ('06_VIX_TERM_PROXY', term_vote), ('07_BREADTH', breadth_vote),
                     ('08_SP500', lambda: trend_vote('sp500')), ('09_SOX', lambda: trend_vote('sox')),
                     ('10_EWT', ewt_vote), ('11_DXY', lambda: trend_vote('dxy', True))]:
        add(name, fn)
    bull, bear = sum(v['bull'] for v in votes), sum(v['bear'] for v in votes)
    complete = all(v['status'] == 'OK' for v in votes)
    return dict(asof=str(asof), mode='NET_VOTING_V1', bull=bull, bear=bear, net=bull-bear,
                coverage=sum(v['status']=='OK' for v in votes), complete=complete,
                regime=classify(bull, bear) if complete else 'INCOMPLETE',
                provisional_regime=classify(bull, bear), analysts=votes,
                note='No continuous score, no orders, no validated trading parameters.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asof', type=date.fromisoformat, default=datetime.now(TW).date()-timedelta(days=1),
                        help='YYYY-MM-DD; default previous Taipei calendar day, end-of-day snapshot')
    parser.add_argument('--input-dir', type=Path, default=BASE/'input')
    parser.add_argument('--output-dir', type=Path, default=BASE/'output')
    parser.add_argument('--config', type=Path, default=BASE/'rules.json')
    parser.add_argument('--offline', action='store_true', help='CSV only; never silently simulate market data')
    args = parser.parse_args()
    if args.asof >= datetime.now(TW).date():
        parser.error('asof must be before today in Taipei; intraday data excluded')
    cfg = DEFAULTS | (json.loads(args.config.read_text(encoding='utf-8')) if args.config.exists() else {})
    cache = BASE/'cache'; data = {}; sources = {}; errors = {}
    keys = list(SYMBOLS) + ['yield_spread', 'pcr', 'breadth']
    for key in keys:
        path = args.input_dir/f'{key}.csv'
        print(f'Loading {key} ...', flush=True)
        try:
            if path.exists():
                rows = read_local(path, args.asof, key=='breadth'); source = str(path)
            elif args.offline:
                raise ValueError(f'CSV not found: {path}')
            elif key in SYMBOLS:
                rows = yahoo(SYMBOLS[key], args.asof, cache); source = 'Yahoo chart: '+SYMBOLS[key]
            elif key == 'yield_spread':
                rows = fred(args.asof, cache); source = 'FRED T10Y2Y'
            elif key == 'pcr':
                rows = pcr(args.asof, cache); source = 'TAIFEX PutCallRatio (volume)'
            else:
                rows = breadth(args.asof, cache, cfg['breadth_calendar_days']); source = 'TWSE MI_INDEX stock counts'
            data[key] = rows
            sources[key] = dict(source=source, rows=len(rows), latest_date=str(rows[-1][0]) if rows else None)
        except Exception as e:
            errors[key] = f'{type(e).__name__}: {e}'
            print(f'  MISSING: {errors[key]}', flush=True)
    report = evaluate(data, args.asof, cfg)
    report.update(config=cfg, sources=sources, fetch_errors=errors,
                  retrieved_at=datetime.now(TW).isoformat())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir/f'regime_{args.asof}.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    out = target.with_suffix('.csv')
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['analyst','status','bull','bear','evidence'])
        for v in report['analysts']:
            w.writerow([v['analyst'],v['status'],v['bull'],v['bear'],json.dumps(v['evidence'],ensure_ascii=False)])
    print(f"\nBull={report['bull']} Bear={report['bear']} Net={report['net']:+d}")
    print(f"Coverage={report['coverage']}/11; Regime={report['regime']}; provisional={report['provisional_regime']}")
    print(f'Report: {target}\nCSV: {out}')
    if not report['complete']:
        print('Incomplete inputs: provisional result is for debugging only. See fetch_errors and analysts.')


if __name__ == '__main__':
    main()
