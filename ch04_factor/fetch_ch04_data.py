"""TWSE/TPEx historical prices and institutional flows. Python 3.10+, no packages.

First run: py fetch_ch04_data.py --probe
Full run:  py fetch_ch04_data.py
Outputs beside this script in ch04_data/. Missing numeric values stay blank.
"""
import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'ch04_data'
CACHE = ROOT / 'raw_v1'
TW = 'https://www.twse.com.tw/rwd/zh/'
TP = 'https://www.tpex.org.tw/www/zh-tw/'


def norm(s):
    return re.sub(r'\s+', '', html.unescape(re.sub('<[^>]*>', '', str(s))))


def day(s):
    s = str(s).strip()
    if re.fullmatch(r'\d{8}', s):
        return datetime.strptime(s, '%Y%m%d').date()
    parts = re.split(r'[/\-]', s)
    y, m, d = map(int, parts)
    return date(y + 1911 if y < 1911 else y, m, d)


def number(s):
    s = str(s).replace(',', '').strip()
    if s in ('', '--', '---', '----', '-', 'N/A', '除權息'):
        return None
    return Decimal(s)


def stock(code):
    # Exercise universe: four-digit codes beginning with 1-9; excludes ETFs,
    # warrants, preferred shares and other longer security codes.
    return re.fullmatch(r'[1-9]\d{3}', str(code).strip()) is not None


def save_json(path, obj):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def request(key, url, params, validate):
    path = CACHE / (key + '.json')
    if path.exists():
        payload = json.loads(path.read_text(encoding='utf-8'))
        validate(payload)
        return payload
    url += '?' + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            time.sleep(3)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=35) as response:
                payload = json.load(response)
            validate(payload)
            save_json(path, payload)
            return payload
        except Exception as exc:
            print(f'  Retry {attempt + 1}/3: {type(exc).__name__}: {exc}', flush=True)
            if attempt == 2:
                raise RuntimeError(f'{key}: {exc}') from exc
            time.sleep(3 * (attempt + 1))


def table(payload, required):
    tables = [payload] + payload.get('tables', [])
    for t in tables:
        fields = [norm(f) for f in t.get('fields', [])]
        if set(required) <= set(fields) and t.get('data'):
            return fields, t['data'], t
    raise ValueError(f'Expected nonempty table with fields {required}; stat={payload.get("stat")}')


def check_date(payload, t, expected):
    actual = t.get('date') or payload.get('date')
    if not actual or day(actual) != expected:
        raise ValueError(f'Response date mismatch: requested {expected}, received {actual}')


def prices(payload, market, d):
    code, name = ('證券代號', '證券名稱') if market == 'TWSE' else ('代號', '名稱')
    close = '收盤價' if market == 'TWSE' else '收盤'
    fields, rows, t = table(payload, [code, name, close, '成交股數'])
    check_date(payload, t, d)
    index = {f: i for i, f in enumerate(fields)}
    result = []
    seen = set()
    for r in rows:
        c = str(r[index[code]]).strip()
        if not stock(c):
            continue
        if c in seen:
            raise ValueError(f'Duplicate price code: {c}')
        seen.add(c)
        def value(f):
            return number(r[index[f]])
        suffix = '價' if market == 'TWSE' else ''
        o, h, l, cl = [value(f + suffix) for f in ('開盤', '最高', '最低', '收盤')]
        volume = value('成交股數')
        if cl is None or cl <= 0:
            status = 'NO_VALID_CLOSE'
        elif volume is None or volume <= 0:
            status = 'NO_TRADE_OR_MISSING_VOLUME'
        else:
            status = 'OK'
        if status == 'OK' and all(v is not None for v in (o, h, l)):
            if not (0 < l <= min(o, cl) <= max(o, cl) <= h):
                raise ValueError(f'Invalid OHLC: {c}')
        result.append(dict(date=d.isoformat(), market=market, code=c,
                           name=str(r[index[name]]).strip(), open=o, high=h,
                           low=l, close=cl, volume_shares=volume, status=status))
    if not result:
        raise ValueError('No four-digit stock rows')
    return result


def institutions(payload, market, d):
    code = '證券代號' if market == 'TWSE' else '代號'
    fields, rows, t = table(payload, [code, '投信買賣超股數'] if market == 'TWSE'
                            else [code, '三大法人買賣超股數合計'])
    check_date(payload, t, d)
    if market == 'TWSE':
        fi = next((i for i, f in enumerate(fields)
                   if f in ('外陸資買賣超股數(不含外資自營商)',
                            '外資及陸資買賣超股數(不含外資自營商)')), None)
        if fi is None:
            raise ValueError('Unknown foreign investor field: ' + repr(fields))
        ti = fields.index('投信買賣超股數')
        di, total = fields.index('自營商買賣超股數'), fields.index('三大法人買賣超股數')
    else:
        expected = ['代號', '名稱'] + ['買進股數', '賣出股數', '買賣超股數'] * 7 + ['三大法人買賣超股數合計']
        if fields != expected:
            raise ValueError('TPEx column layout changed: ' + repr(fields))
        fi, ti, di, total = 4, 13, 22, 23
    result, seen = [], set()
    for r in rows:
        c = str(r[0]).strip()
        if not stock(c):
            continue
        if c in seen:
            raise ValueError(f'Duplicate institutional code: {c}')
        seen.add(c)
        f, trust, dealer, combined = [number(r[i]) for i in (fi, ti, di, total)]
        if any(x is None for x in (f, trust, dealer, combined)):
            raise ValueError(f'Missing institutional value: {c}')
        if f + trust + dealer != combined:
            raise ValueError(f'Institutional total mismatch: {c}')
        # Both reports provide the net column immediately after buy and sell.
        for i in (fi, ti):
            if number(r[i-2]) - number(r[i-1]) != number(r[i]):
                raise ValueError(f'Buy/sell/net mismatch: {c}')
        result.append(dict(date=d.isoformat(), market=market, code=c,
                           name=str(r[1]).strip(), foreign_net_shares=f,
                           trust_net_shares=trust, inst_net_shares=f + trust,
                           inst_net_lots=(f + trust) / Decimal(1000), status='OK'))
    if not result:
        raise ValueError('No institutional stock rows')
    return result


def trading_days(start, end):
    found = set()
    m = start.replace(day=1)
    while m <= end:
        print(f'Calendar {m:%Y-%m}', flush=True)
        def validate(p):
            if p.get('stat') != 'OK' or not p.get('data'):
                raise ValueError('Trading calendar unavailable; do not assume a holiday')
            if any(day(r[0]).replace(day=1) != m for r in p['data']):
                raise ValueError('Calendar month mismatch')
        p = request(f'calendar_{m:%Y%m}', TW + 'afterTrading/FMTQIK',
                    {'date': f'{m:%Y%m%d}', 'response': 'json'}, validate)
        found.update(day(r[0]) for r in p['data'] if start <= day(r[0]) <= end)
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    if not found:
        raise ValueError('No trading dates in selected range')
    return sorted(found)


def write_csv(path, rows, fields):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start', type=date.fromisoformat, default=date(2026, 5, 1))
    p.add_argument('--end', type=date.fromisoformat, default=date(2026, 11, 12))
    p.add_argument('--probe', action='store_true', help='Download only the latest selected trading date')
    args = p.parse_args()
    today_tw = datetime.now(timezone(timedelta(hours=8))).date()
    if not args.start <= args.end < today_tw:
        p.error('Require start <= end < today (Taiwan time); use a completed prior day')
    CACHE.mkdir(parents=True, exist_ok=True)
    dates = trading_days(args.end.replace(day=1) if args.probe else args.start, args.end)
    if args.probe:
        dates = dates[-1:]
    inst_dates = dates[-10:]
    tasks = [(d, m, 'prices') for d in dates for m in ('TWSE', 'TPEX')]
    tasks += [(d, m, 'institutions') for d in inst_dates for m in ('TWSE', 'TPEX')]
    all_prices, all_inst, errors = [], [], []
    for i, (d, market, kind) in enumerate(tasks, 1):
        print(f'[{i}/{len(tasks)}] {market} {kind} {d}', flush=True)
        if market == 'TWSE':
            url = TW + ('afterTrading/MI_INDEX' if kind == 'prices' else 'fund/T86')
            params = {'date': f'{d:%Y%m%d}', 'response': 'json'}
            params['type' if kind == 'prices' else 'selectType'] = 'ALLBUT0999'
        else:
            url = TP + ('afterTrading/otc' if kind == 'prices' else 'insti/dailyTrade')
            params = {'date': f'{d:%Y/%m/%d}', 'response': 'json'}
            params.update({'type': 'AL'} if kind == 'prices'
                          else {'type': 'Daily', 'sect': 'EW', 'id': ''})
        parser = prices if kind == 'prices' else institutions
        try:
            payload = request(f'{market}_{kind}_{d:%Y%m%d}', url, params,
                              lambda x: parser(x, market, d))
            rows = parser(payload, market, d)
            (all_prices if kind == 'prices' else all_inst).extend(rows)
            print(f'  OK: {len(rows)} rows', flush=True)
        except Exception as exc:
            errors.append(dict(date=d.isoformat(), market=market, dataset=kind,
                               status='FETCH_FAILED', error=str(exc)))
            print(f'  FAILED: {exc}', flush=True)
            # Stop on first failure: avoids hundreds of requests to a broken endpoint.
            break
    tag = 'probe' if args.probe else f'{args.start:%Y%m%d}_{args.end:%Y%m%d}'
    out = ROOT / tag
    out.mkdir(exist_ok=True)
    price_fields = 'date market code name open high low close volume_shares status'.split()
    inst_fields = 'date market code name foreign_net_shares trust_net_shares inst_net_shares inst_net_lots status'.split()
    write_csv(out / 'prices.csv', all_prices, price_fields)
    write_csv(out / 'institutions.csv', all_inst, inst_fields)
    write_csv(out / 'fetch_errors.csv', errors, 'date market dataset status error'.split())
    write_csv(out / 'trading_dates.csv', [dict(date=d.isoformat(), institutional_target=d in inst_dates)
                                        for d in dates], ['date', 'institutional_target'])
    summary = dict(status='INCOMPLETE' if errors else 'COMPLETE', probe=args.probe,
                   start=str(args.start), requested_end=str(args.end), latest_trade_date=str(dates[-1]),
                   price_trading_days=len(dates), institution_trading_days=len(inst_dates),
                   price_rows=len(all_prices), institution_rows=len(all_inst), errors=errors,
                   universe='four-digit codes 1000-9999; not an official security-type classification',
                   prices='unadjusted; no zero filling; no suspension/listing classification inferred',
                   foreign='foreign/mainland excluding foreign dealers; 1 lot = 1000 shares')
    save_json(out / 'summary.json', summary)
    print(f'\nStatus: {summary["status"]}; prices={len(all_prices)}, institutions={len(all_inst)}')
    print(f'Output: {out}')
    if errors:
        print('Do not import incomplete CSVs. Fix the error and rerun the same command; validated raw data is reused.')
        return 1
    if not args.probe and len(dates) < 60:
        print('WARNING: fewer than 60 market sessions; extend --start.')
    print('Raw downloads cached. Run again to resume/rebuild without downloading validated days again.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nStopped. Rerun the same command to resume from validated downloads.')
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
