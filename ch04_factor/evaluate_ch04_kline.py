r"""Read-only SQL event study: bullish real-body engulfing, fixed 5-session horizon.
Run: py .\ch04_factor\evaluate_ch04_kline.py
Requires pandas and pyodbc. Does not change factors, weights or SQL.
"""
import argparse
import json
import os
from pathlib import Path

import pandas as pd
import pyodbc

KEYS = ['date', 'market', 'code']
RULE = {
    'pattern': 'bullish_body_engulfing_v1_no_trend_filter',
    'definition': 'previous close < previous open; close > open; open <= previous close; close >= previous open; at least one engulfing boundary strict',
    'hypothesis': 'signals have higher mean next-session-open to session-t+5-close return than same-date same-market non-signals',
    'horizon': 5,
    'universe': '60 consecutive valid positive-volume closes through signal date; both adjacent OHLC candles valid',
    'primary_return': 'close(t+5)/open(t+1)-1; valid endpoints; no filling',
    'control': 'equal-weight non-signals on same date and market; identical entry/exit availability rules',
    'split': 'first 70% of post-warmup market dates research; remaining 30% holdout; research outcomes crossing holdout start embargoed',
    'limitations': ['Unadjusted prices; corporate actions may distort pattern and return',
        'Open price is an execution proxy; no guarantee of fill, no costs or price-limit checks',
        'Overlapping horizons and repeated stocks are dependent; no significance claims',
        'Unavailable endpoint returns excluded; missing coverage is reported',
        'No downtrend filter; tests this exact candle geometry, not all engulfing definitions',
        'Event difference is an association, not proof of causality or incremental strategy value'],
}


def read_sql(cur, sql):
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)


def analyze(prices):
    p = prices.copy()
    if p.empty or p.duplicated(KEYS).any():
        raise ValueError('Empty prices or duplicate keys')
    p['date'] = pd.to_datetime(p['date'])
    calendar = pd.DatetimeIndex(sorted(p['date'].unique()), name='date')
    if len(calendar) < 70:
        raise ValueError('Need >=70 market sessions including warmup')
    for c in ['open', 'high', 'low', 'close', 'volume_shares']:
        p[c] = pd.to_numeric(p[c], errors='raise')
    # Reindex each stock to the market calendar: absent sessions cannot be skipped.
    frames = []
    research_dates = calendar[59:]
    cut = max(1, int(len(research_dates) * .70))
    holdout_start = research_dates[cut]
    next_dates = pd.Series(calendar, index=calendar).shift(-1)
    exits = pd.Series(calendar, index=calendar).shift(-5)
    for (market, code), g in p.groupby(['market', 'code'], sort=True):
        g = g.set_index('date').reindex(calendar)
        present = g['code'].notna()
        valid_close = g['status'].eq('OK') & g['close'].gt(0) & g['volume_shares'].gt(0)
        valid_ohlc = (valid_close & g[['open', 'high', 'low', 'close']].notna().all(axis=1)
            & g[['open', 'high', 'low', 'close']].gt(0).all(axis=1)
            & g['high'].ge(g[['open', 'close']].max(axis=1))
            & g['low'].le(g[['open', 'close']].min(axis=1)) & g['high'].ge(g['low']))
        ready = valid_close.rolling(60, min_periods=60).sum().eq(60)
        available = present & ready & valid_ohlc & valid_ohlc.shift(1, fill_value=False)
        prev_open, prev_close = g['open'].shift(1), g['close'].shift(1)
        hit = (available & prev_close.lt(prev_open) & g['close'].gt(g['open'])
            & g['open'].le(prev_close) & g['close'].ge(prev_open)
            & (g['open'].lt(prev_close) | g['close'].gt(prev_open)))
        entry = g['open'].shift(-1).where(valid_ohlc.shift(-1, fill_value=False))
        exit_close = g['close'].shift(-5).where(valid_close.shift(-5, fill_value=False))
        ret = exit_close.div(entry).sub(1)
        result = pd.DataFrame(dict(market=market, code=code, name=g['name'],
            eligible=available, hit=hit, entry_date=next_dates, exit_date=exits,
            entry_open=entry, exit_close=exit_close, forward_return=ret), index=calendar)
        result['phase'] = 'warmup'
        result.loc[result.index >= research_dates[0], 'phase'] = 'research'
        result.loc[result.index >= holdout_start, 'phase'] = 'holdout'
        result.loc[(result['phase']=='research') & result['exit_date'].ge(holdout_start), 'phase'] = 'embargo'
        result['outcome_status'] = 'OK'
        result.loc[result['forward_return'].isna(), 'outcome_status'] = 'MISSING_ENTRY_OR_EXIT'
        result.loc[result['exit_date'].isna(), 'outcome_status'] = 'FORWARD_WINDOW_NOT_FINISHED'
        frames.append(result.loc[present].reset_index())
    signals = pd.concat(frames, ignore_index=True)
    study = signals.loc[signals['eligible'] & signals['phase'].isin(['research', 'holdout'])]
    daily_rows, event_frames = [], []
    for (phase, day, market), g in study.groupby(['phase', 'date', 'market'], sort=True):
        hits = g.loc[g['hit']]
        usable = g.loc[g['outcome_status'].eq('OK')]
        hit_returns = usable.loc[usable['hit'], 'forward_return']
        controls = usable.loc[~usable['hit'], 'forward_return']
        control_mean = controls.mean()
        difference = hit_returns.mean() - control_mean
        daily_rows.append(dict(phase=phase, date=day, market=market,
            candidate_hits=len(hits), usable_hits=len(hit_returns), controls=len(controls),
            unavailable_hits=len(hits)-len(hit_returns),
            hit_mean_return=hit_returns.mean(), control_mean_return=control_mean,
            hit_win_fraction=hit_returns.gt(0).mean() if len(hit_returns) else None,
            difference=difference))
        events = hits.copy()
        events['control_mean_return'] = control_mean
        events['excess_return'] = events['forward_return'] - control_mean
        event_frames.append(events)
    daily = pd.DataFrame(daily_rows)
    events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    summary = []
    for phase in ['research', 'holdout']:
        for market in ['TWSE', 'TPEX']:
            d = daily.loc[daily['phase'].eq(phase) & daily['market'].eq(market)]
            e = events.loc[events['phase'].eq(phase) & events['market'].eq(market)] if not events.empty else events
            valid = e.dropna(subset=['forward_return']) if not e.empty else e
            paired = e.dropna(subset=['excess_return']) if not e.empty else e
            summary.append(dict(phase=phase, market=market, candidate_hits=len(e),
                usable_hits=len(valid), paired_hits=len(paired),
                unique_hit_stocks=e['code'].nunique() if len(e) else 0,
                comparable_signal_dates=d['difference'].notna().sum(),
                mean_hit_return=valid['forward_return'].mean() if len(valid) else None,
                win_fraction=valid['forward_return'].gt(0).mean() if len(valid) else None,
                event_weighted_excess=paired['excess_return'].mean() if len(paired) else None,
                date_equal_weighted_difference=d['difference'].mean(),
                positive_difference_date_fraction=d.loc[d['difference'].notna(), 'difference'].gt(0).mean(),
                unavailable_hits=len(e)-len(valid)))
    return signals, events, daily, pd.DataFrame(summary), holdout_start


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'ch04_kline_event')
    args = ap.parse_args()
    driver = os.getenv('MINIQUANT_SQL_DRIVER') or next((d for d in [
        'ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server'] if d in pyodbc.drivers()), None)
    if not driver:
        raise ValueError('SQL Server ODBC Driver 17/18 required')
    server = os.getenv('MINIQUANT_SQL_SERVER', r'localhost\SQLEXPRESS')
    database = os.getenv('MINIQUANT_SQL_DATABASE', 'MiniQuantDemo')
    conn = pyodbc.connect(f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;', timeout=15)
    try:
        prices = read_sql(conn.cursor(), 'SELECT [date],market,code,name,[open],high,low,[close],volume_shares,status FROM dbo.ch04_prices')
    finally:
        conn.close()
    print(f'Read SQL: prices={len(prices):,}', flush=True)
    signals, events, daily, summary, split = analyze(prices)
    out = args.output / (pd.to_datetime(prices['date']).max().strftime('%Y%m%d') + '_bullish_engulfing_v1')
    out.mkdir(parents=True, exist_ok=True)
    for name, df in [('signals_all', signals), ('events', events), ('event_daily', daily), ('event_summary', summary)]:
        df.to_csv(out / (name+'.csv'), index=False, encoding='utf-8-sig')
    latest = signals.loc[signals['date'].eq(signals['date'].max()) & signals['hit']]
    latest.to_csv(out / 'latest_hits.csv', index=False, encoding='utf-8-sig')
    settings = dict(RULE, holdout_start=split.date().isoformat(),
        source_server=server, source_database=database, rows=len(prices),
        start_date=str(min(prices['date'])), end_date=str(max(prices['date'])))
    (out/'event_settings.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Primary: next open -> signal-date +5 market sessions close; returns are decimals.')
    print('Holdout starts:', split.date().isoformat())
    print(summary.to_string(index=False, float_format=lambda x: f'{x:.6f}'))
    print(f'Latest pattern hits: {len(latest)} (pattern list, not a stock recommendation)')
    print('Output:', out.resolve())
    print('DONE: SQL read only. No changes to three-factor weights or ranking.')


if __name__ == '__main__':
    main()
