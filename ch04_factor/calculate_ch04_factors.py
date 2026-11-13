r"""Chapter 4: Wilder RSI14, SMA5/20/60 and institutional scoring.

Run beside fetch_ch04_data.py:
    py .\ch04_factor\calculate_ch04_factors.py
No network, extra packages, or SQL writes. CSV output uses UTF-8 with BOM.
"""
import argparse
import csv
import json
import math
import sys
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

WEIGHTS = {'rsi': 0.40, 'inst': 0.35, 'ma': 0.25}
KEYS = ('date', 'market', 'code')
FIELDS = '''date market code name close rsi14 ma5 ma20 ma60
valid_days_last60 contiguous_valid_days foreign_net_shares trust_net_shares
inst_net_lots score_rsi score_inst score_ma weighted_rsi weighted_inst weighted_ma
total_score eligible exclusion_reason price_status'''.split()


def read_csv(path, required):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not set(required) <= set(reader.fieldnames or []):
            raise ValueError(f'{path.name}: missing columns {set(required) - set(reader.fieldnames or [])}')
        return list(reader)


def numeric(value):
    if value is None or str(value).strip() == '':
        return None
    v = float(str(value).replace(',', ''))
    if not math.isfinite(v):
        raise ValueError(f'Non-finite numeric value: {value}')
    return v


def unique_index(rows, title):
    out = {}
    for r in rows:
        key = tuple(r[k] for k in KEYS)
        if key in out:
            raise ValueError(f'{title}: duplicate key {key}')
        out[key] = r
    return out


def score_rsi(rsi):
    if rsi is None:
        return None
    if rsi >= 80 or rsi <= 20:
        return 2.0
    return 9.0 if 45 <= rsi <= 65 else 6.0


def score_inst(lots):
    if lots is None:
        return None
    return 3.0 if lots <= 0 else min(10.0, 5.0 + lots / 1000.0)


def score_ma(close, ma5, ma20, ma60):
    if any(x is None for x in (close, ma5, ma20, ma60)):
        return None
    if close > ma5 > ma20 > ma60:
        return 10.0
    return 6.0 if close > ma20 else 3.0


class IndicatorState:
    """One state per market/code; reset RSI at any invalid/missing market session."""
    def __init__(self):
        self.window = deque(maxlen=60)
        self.previous = None
        self.gains, self.losses = [], []
        self.avg_gain = self.avg_loss = None
        self.streak = 0

    def update(self, close):
        self.window.append(close)
        if close is None:
            self.previous = None
            self.gains.clear()
            self.losses.clear()
            self.avg_gain = self.avg_loss = None
            self.streak = 0
            return None
        self.streak += 1
        rsi = None
        if self.previous is not None:
            delta = close - self.previous
            gain, loss = max(delta, 0.0), max(-delta, 0.0)
            if self.avg_gain is None:
                self.gains.append(gain)
                self.losses.append(loss)
                if len(self.gains) == 14:
                    self.avg_gain = sum(self.gains) / 14
                    self.avg_loss = sum(self.losses) / 14
            else:
                self.avg_gain = (self.avg_gain * 13 + gain) / 14
                self.avg_loss = (self.avg_loss * 13 + loss) / 14
            if self.avg_gain is not None:
                # Explicit convention for a completely flat initial window.
                if self.avg_gain == 0 and self.avg_loss == 0:
                    rsi = 50.0
                elif self.avg_loss == 0:
                    rsi = 100.0
                else:
                    rsi = 100 - 100 / (1 + self.avg_gain / self.avg_loss)
        self.previous = close
        return rsi

    def ma(self, n):
        values = list(self.window)[-n:]
        if len(values) != n or any(x is None for x in values):
            return None
        return sum(values) / n


def factor_rows(prices, inst, dates):
    inst_index = unique_index(inst, 'institutions')
    unique_index(prices, 'prices')
    expected = set(dates)
    groups = defaultdict(dict)
    for r in prices:
        if r['date'] not in expected:
            raise ValueError(f'Price date outside calendar: {r["date"]}')
        groups[(r['market'], r['code'])][r['date']] = r
    for r in inst:
        if r['date'] not in expected:
            raise ValueError('Institutional date outside calendar')
        f, t = numeric(r['foreign_net_shares']), numeric(r['trust_net_shares'])
        lots = numeric(r['inst_net_lots'])
        if all(x is not None for x in (f, t, lots)) and not math.isclose(
                (f + t) / 1000, lots, abs_tol=1e-7, rel_tol=1e-10):
            raise ValueError(f'Institutional unit mismatch: {r["date"]} {r["code"]}')
    for (market, code), by_date in sorted(groups.items()):
        state = IndicatorState()
        for position, d in enumerate(dates):
            p = by_date.get(d)
            close = numeric(p['close']) if p else None
            volume = numeric(p['volume_shares']) if p else None
            price_ok = bool(p and p['status'] == 'OK' and close is not None
                            and close > 0 and volume is not None and volume > 0)
            rsi = state.update(close if price_ok else None)
            if p is None:
                continue  # The calendar gap resets state, but no invented price row.
            ma5, ma20, ma60 = [state.ma(n) for n in (5, 20, 60)]
            valid60 = sum(x is not None for x in state.window)
            ins = inst_index.get((d, market, code))
            foreign = numeric(ins['foreign_net_shares']) if ins else None
            trust = numeric(ins['trust_net_shares']) if ins else None
            lots = numeric(ins['inst_net_lots']) if ins else None
            if ins and ins.get('status') != 'OK':
                lots = None
            if foreign is None or trust is None:
                lots = None
            sr, si, sm = score_rsi(rsi), score_inst(lots), score_ma(
                close if price_ok else None, ma5, ma20, ma60)
            reasons = []
            if not price_ok:
                reasons.append('NO_VALID_PRICE_TODAY')
            if position < 59:
                reasons.append('LESS_THAN_60_MARKET_DAYS_IN_DATASET')
            if valid60 < min(position + 1, 60):
                reasons.append('INCOMPLETE_VALID_PRICES_LAST60')
            if rsi is None:
                reasons.append('RSI14_UNAVAILABLE')
            if lots is None:
                reasons.append('INSTITUTIONS_UNAVAILABLE')
            eligible = not reasons and all(x is not None for x in (sr, si, sm))
            wr = sr * WEIGHTS['rsi'] if sr is not None else None
            wi = si * WEIGHTS['inst'] if si is not None else None
            wm = sm * WEIGHTS['ma'] if sm is not None else None
            yield dict(date=d, market=market, code=code, name=p['name'], close=close,
                       rsi14=rsi, ma5=ma5, ma20=ma20, ma60=ma60,
                       valid_days_last60=valid60, contiguous_valid_days=state.streak,
                       foreign_net_shares=foreign, trust_net_shares=trust,
                       inst_net_lots=lots, score_rsi=sr, score_inst=si, score_ma=sm,
                       weighted_rsi=wr, weighted_inst=wi, weighted_ma=wm,
                       total_score=round(wr + wi + wm, 12) if eligible else None,
                       eligible=eligible, exclusion_reason=';'.join(reasons),
                       price_status=p['status'])


def rank_rows(rows):
    ranked = sorted((dict(r) for r in rows if r['eligible']),
                    key=lambda r: (-r['total_score'], r['market'], r['code']))
    last = None
    rank = 0
    for position, r in enumerate(ranked, 1):
        if r['total_score'] != last:
            rank = position  # Competition ranking: 1, 1, 3.
            last = r['total_score']
        r['rank'] = rank
    return ranked


def write_csv(path, rows, fields):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k) for k in fields})
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path,
                        default=Path(__file__).resolve().parent / 'ch04_data' / '20260501_20261112')
    parser.add_argument('--as-of', type=date.fromisoformat, help='Optional historical date; no future rows used')
    args = parser.parse_args()
    folder = args.data_dir.resolve()
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
    if summary.get('status') != 'COMPLETE' or summary.get('probe'):
        raise ValueError('Require a COMPLETE full download, not probe data.')
    prices = read_csv(folder / 'prices.csv', list(KEYS) + ['name', 'close', 'volume_shares', 'status'])
    inst = read_csv(folder / 'institutions.csv', list(KEYS) + [
        'foreign_net_shares', 'trust_net_shares', 'inst_net_lots', 'status'])
    cal = read_csv(folder / 'trading_dates.csv', ['date'])
    if len({r['date'] for r in cal}) != len(cal):
        raise ValueError('Duplicate calendar dates')
    dates = sorted(r['date'] for r in cal)
    for d in dates:
        date.fromisoformat(d)
    if args.as_of:
        dates = [d for d in dates if d <= args.as_of.isoformat()]
    if len(dates) < 60:
        raise ValueError('Need at least 60 market sessions')
    latest = dates[-1]
    prices = [r for r in prices if r['date'] <= latest]
    inst = [r for r in inst if r['date'] <= latest]
    for market in ('TWSE', 'TPEX'):
        actual = {r['date'] for r in prices if r['market'] == market}
        if set(dates) - actual:
            raise ValueError(f'{market}: entire price dates missing: {sorted(set(dates) - actual)}')
    out = folder / ('factors_' + latest.replace('-', ''))
    out.mkdir(exist_ok=True)
    today = []
    # Keep point-in-time daily factors for the next IC exercise. This is not an IC calculation.
    def historical():
        for r in factor_rows(prices, inst, dates):
            if r['date'] == latest:
                today.append(r)
            yield r
    write_csv(out / 'factor_history.csv', historical(), FIELDS)
    ranked = rank_rows(today)
    excluded = [r for r in today if not r['eligible']]
    write_csv(out / 'latest_all.csv', today, FIELDS)
    write_csv(out / 'ranking.csv', ranked, ['rank'] + FIELDS)
    write_csv(out / 'excluded.csv', excluded, FIELDS)
    settings = dict(as_of=latest, weights=WEIGHTS, latest_rows=len(today),
                    ranked=len(ranked), excluded=len(excluded),
                    eligibility='60 consecutive market sessions with valid close and positive volume; all three factors present',
                    rsi='Wilder14: first 14 changes SMA seed, then (old*13+new)/14; reset on gaps; flat gain/loss=50',
                    ma='simple moving averages, includes as-of close, no filling',
                    price_basis='unadjusted official closes; corporate actions not adjusted',
                    tie_rule='same full score same rank; market/code order inside ties is display only',
                    total_score='full precision for scoring; stored at 12 decimals; console shows 4',
                    scope='daily factors and teaching ranking only; no IC, backtest or SQL write')
    (out / 'calculation_settings.json').write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'評分日期：{latest}；最新日 {len(today)} 檔；納入 {len(ranked)} 檔；未納入 {len(excluded)} 檔')
    print('權重：RSI 40% / 法人 35% / 均線 25%')
    print('排名以未四捨五入的指標計分；同分同名次，代號順序不表示優劣。')
    print('\n前20筆（若第20筆仍同分，完整同分名單請看 ranking.csv）：')
    print('rank market code name RSI14 inst_lots RSI分 法人分 均線分 total')
    for r in ranked[:20]:
        print(f'{r["rank"]:>4} {r["market"]:4} {r["code"]} {r["name"]} '
              f'{r["rsi14"]:.2f} {r["inst_net_lots"]:.3f} '
              f'{r["score_rsi"]:.1f} {r["score_inst"]:.4f} {r["score_ma"]:.1f} '
              f'{r["total_score"]:.4f}')
    print(f'\n輸出：{out}')
    print('本次使用未還原股價，除權息或分割可能影響指標；結果為教學規則排名，尚未驗證報酬。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
