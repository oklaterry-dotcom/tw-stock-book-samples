r"""Read chapter 4 SQL tables; write daily Rank IC and coverage CSVs. No SQL writes.
Run: py .\ch04_factor\evaluate_ch04_ic.py
"""
import argparse
import json
import math
import os
from pathlib import Path

import pandas as pd
import pyodbc

METRICS = ['rsi14', 'inst_net_lots', 'score_rsi', 'score_inst', 'score_ma', 'total_score']


def read_table(cur, query, params=()):
    cur.execute(query, *params)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)


def evaluate(prices, factors, horizons=(1, 5, 10), min_stocks=30):
    keys = ['date', 'market', 'code']
    if prices.duplicated(keys).any() or factors.duplicated(keys).any():
        raise ValueError('Duplicate input keys')
    prices = prices.copy()
    factors = factors.copy()
    for df in (prices, factors):
        df['date'] = pd.to_datetime(df['date'])
    calendar = pd.DatetimeIndex(sorted(prices['date'].unique()), name='date')
    if calendar.empty:
        raise ValueError('Empty price calendar')
    # Market-session offsets, never next available stock row.
    prices['close'] = pd.to_numeric(prices['close'], errors='raise')
    prices['volume_shares'] = pd.to_numeric(prices['volume_shares'], errors='raise')
    valid = prices['status'].eq('OK') & prices['close'].gt(0) & prices['volume_shares'].gt(0)
    prices['valid_close'] = prices['close'].where(valid)
    panel = prices.pivot(index='date', columns=['market', 'code'], values='valid_close').reindex(calendar)
    data = factors.set_index(keys)
    rows = []
    for horizon in horizons:
        returns = panel.shift(-horizon).div(panel).sub(1)
        long = returns.stack(['market', 'code'], future_stack=True).dropna().rename('forward_return')
        joined = data.join(long, how='left').reset_index()
        targets = {d: calendar[i+horizon] if i+horizon < len(calendar) else pd.NaT
                   for i, d in enumerate(calendar)}
        for day, group in joined.groupby('date', sort=True):
            target = targets.get(day, pd.NaT)
            ready = pd.to_numeric(group['valid_days_last60']).eq(60) & group['price_status'].eq('OK')
            for metric in METRICS:
                x = pd.to_numeric(group[metric], errors='raise')
                mask = ready & x.notna()
                if metric == 'total_score':
                    mask &= group['eligible'].eq(True)
                candidates = int(mask.sum())
                pairs = mask & group['forward_return'].notna()
                n = int(pairs.sum())
                xs, ys = x[pairs], group.loc[pairs, 'forward_return']
                nx, ny = xs.nunique(), ys.nunique()
                reason = ''
                if pd.isna(target):
                    reason = 'FORWARD_WINDOW_NOT_FINISHED'
                elif candidates == 0:
                    reason = 'NO_FACTOR_OBSERVATIONS'
                elif n < min_stocks:
                    reason = 'TOO_FEW_STOCKS'
                elif nx < 2:
                    reason = 'CONSTANT_FACTOR'
                elif ny < 2:
                    reason = 'CONSTANT_RETURN'
                ic = None if reason else xs.rank(method='average').corr(ys.rank(method='average'))
                if ic is not None and not math.isfinite(ic):
                    reason, ic = 'UNDEFINED_CORRELATION', None
                rows.append(dict(date=day.date().isoformat(), horizon=horizon, metric=metric,
                    target_date='' if pd.isna(target) else target.date().isoformat(),
                    candidate_stocks=candidates, paired_stocks=n, missing_return=candidates-n,
                    distinct_factor_values=nx, rank_ic=ic, skip_reason=reason))
    daily = pd.DataFrame(rows)
    summaries = []
    for (metric, horizon), group in daily.groupby(['metric', 'horizon'], sort=True):
        usable = group.dropna(subset=['rank_ic'])
        series = usable['rank_ic']
        summaries.append(dict(metric=metric, horizon=horizon, valid_dates=len(usable),
            first_date=usable['date'].min() if len(usable) else '',
            last_date=usable['date'].max() if len(usable) else '',
            mean_rank_ic=series.mean(), median_rank_ic=series.median(),
            std_rank_ic=series.std(ddof=1),
            positive_date_fraction=series.gt(0).mean() if len(usable) else None,
            mean_paired_stocks=usable['paired_stocks'].mean(),
            unfinished_dates=int(group['skip_reason'].eq('FORWARD_WINDOW_NOT_FINISHED').sum()),
            note='DESCRIPTIVE_ONLY; overlapping horizons; unadjusted prices'))
    return daily, pd.DataFrame(summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', help='Exact ch04_versions.version_id; required if multiple versions exist')
    parser.add_argument('--min-stocks', type=int, default=30)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'ch04_ic')
    args = parser.parse_args()
    if args.min_stocks < 3:
        raise ValueError('min-stocks must be at least 3')
    driver = os.getenv('MINIQUANT_SQL_DRIVER') or next((d for d in [
        'ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server'] if d in pyodbc.drivers()), None)
    if not driver:
        raise ValueError('SQL Server ODBC Driver 17/18 required')
    server = os.getenv('MINIQUANT_SQL_SERVER', r'localhost\SQLEXPRESS')
    database = os.getenv('MINIQUANT_SQL_DATABASE', 'MiniQuantDemo')
    conn = pyodbc.connect(f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;', timeout=15)
    try:
        cur = conn.cursor()
        versions = read_table(cur, 'SELECT version_id, metadata FROM dbo.ch04_versions')
        if args.version:
            versions = versions.loc[versions['version_id'].eq(args.version)]
        if len(versions) != 1:
            raise ValueError('Select exactly one version using --version. Available: ' + ', '.join(versions['version_id']))
        version = versions.iloc[0]['version_id']
        metadata = json.loads(versions.iloc[0]['metadata'])
        factors = read_table(cur, 'SELECT * FROM dbo.ch04_factors WHERE version_id=?', (version,))
        if factors.empty:
            raise ValueError('No factor data')
        prices = read_table(cur, 'SELECT [date], market, code, [close], volume_shares, status FROM dbo.ch04_prices WHERE [date] >= ? AND [date] <= ?',
                            (min(factors['date']), max(factors['date'])))
    finally:
        conn.close()
    print(f'Read SQL: prices={len(prices):,}; factors={len(factors):,}', flush=True)
    daily, summary = evaluate(prices, factors, min_stocks=args.min_stocks)
    out = args.output / version[:12]
    out.mkdir(parents=True, exist_ok=True)
    daily.to_csv(out / 'ic_daily.csv', index=False, encoding='utf-8-sig')
    summary.to_csv(out / 'ic_summary.csv', index=False, encoding='utf-8-sig')
    settings = dict(version_id=version, source_metadata=metadata,
        min_stocks=args.min_stocks, horizons=[1, 5, 10],
        return_definition='close(t+h)/close(t)-1 using market sessions; requires valid traded endpoint prices; no filling',
        universe='each date: 60 valid market sessions, own metric available; total also requires eligible=True',
        comparison='metrics have different date/stock coverage; mean IC values are not a matched-sample comparison',
        correlation='cross-sectional Spearman via average ranks; daily means equally weighted',
        limitations=['Unadjusted prices: corporate actions can distort returns and factors',
            'Close-to-close prediction diagnostic, not executable trading return',
            'Missing future endpoints excluded; check paired coverage',
            'Overlapping horizons are not independent observations',
            'No significance claim, factor selection or weight optimization'])
    (out / 'ic_settings.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    print(summary[['metric', 'horizon', 'valid_dates', 'mean_rank_ic', 'positive_date_fraction']].to_string(index=False))
    print('Output:', out.resolve())
    print('DONE: SQL read only. Descriptive exercise; no weight changes.')


if __name__ == '__main__':
    main()
