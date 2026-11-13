r"""Read-only SQL batch study: nine fixed candlestick definitions, five-session horizon.
Run: py .\ch04_factor\evaluate_ch04_klines.py
Requires pandas and pyodbc. Does not change factors, weights or SQL.
"""
import argparse
import json
import os
from pathlib import Path

import pandas as pd
import pyodbc

KEYS = ['date', 'market', 'code']
PATTERNS = {
    'bullish_engulfing': ('bullish', 'Prev bearish, today bullish; O<=prev C, C>=prev O, at least one strict; no trend filter'),
    'bearish_engulfing': ('bearish', 'Prev bullish, today bearish; O>=prev C, C<=prev O, at least one strict; no trend filter'),
    'hammer': ('bullish', 'Body/range 0.10..0.35; lower>=2*body; upper<=0.5*body; prev C<C six sessions ago'),
    'shooting_star': ('bearish', 'Body/range 0.10..0.35; upper>=2*body; lower<=0.5*body; prev C>C six sessions ago'),
    'bullish_harami': ('bullish', 'Prev bearish, today bullish; real body strictly inside previous body; no trend filter'),
    'bearish_harami': ('bearish', 'Prev bullish, today bearish; real body strictly inside previous body; no trend filter'),
    'morning_star': ('bullish', 'Three candles: first bearish body/range>=0.5; middle body<=0.3*first body and body top<first midpoint; third bullish close>first midpoint; no gap/trend requirement'),
    'evening_star': ('bearish', 'Three candles: first bullish body/range>=0.5; middle body<=0.3*first body and body bottom>first midpoint; third bearish close<first midpoint; no gap/trend requirement'),
    'doji': ('neutral', 'Positive range; body/range<=0.10; no directional assumption'),
}


def detect(g, valid):
    o, h, l, c = [g[x] for x in ['open', 'high', 'low', 'close']]
    body, span = (c-o).abs(), h-l
    upper, lower = h-pd.concat([o,c], axis=1).max(axis=1), pd.concat([o,c], axis=1).min(axis=1)-l
    po, pc = o.shift(1), c.shift(1)
    two = valid & valid.shift(1, fill_value=False)
    three = two & valid.shift(2, fill_value=False)
    fbody, fspan = body.shift(2), span.shift(2)
    mid = (o.shift(2)+c.shift(2))/2
    prior5 = c.shift(1)-c.shift(6)
    masks = {
      'bullish_engulfing': two & (pc<po) & (c>o) & (o<=pc) & (c>=po) & ((o<pc)|(c>po)),
      'bearish_engulfing': two & (pc>po) & (c<o) & (o>=pc) & (c<=po) & ((o>pc)|(c<po)),
      'hammer': valid & span.gt(0) & (body>=.10*span) & (body<=.35*span) & (lower>=2*body) & (upper<=.5*body) & prior5.lt(0),
      'shooting_star': valid & span.gt(0) & (body>=.10*span) & (body<=.35*span) & (upper>=2*body) & (lower<=.5*body) & prior5.gt(0),
      'bullish_harami': two & (pc<po) & (c>o) & (o>pc) & (c<po),
      'bearish_harami': two & (pc>po) & (c<o) & (o<pc) & (c>po),
      'morning_star': three & (c.shift(2)<o.shift(2)) & fspan.gt(0) & (fbody>=.5*fspan) & (body.shift(1)<=.3*fbody) & (pd.concat([po,pc],axis=1).max(axis=1)<mid) & (c>o) & (c>mid),
      'evening_star': three & (c.shift(2)>o.shift(2)) & fspan.gt(0) & (fbody>=.5*fspan) & (body.shift(1)<=.3*fbody) & (pd.concat([po,pc],axis=1).min(axis=1)>mid) & (c<o) & (c<mid),
      'doji': valid & span.gt(0) & (body<=.10*span),
    }
    return masks


def read_sql(cur, sql):
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)


def build_signals(prices):
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
        masks = detect(g, valid_ohlc)
        entry = g['open'].shift(-1).where(valid_ohlc.shift(-1, fill_value=False))
        exit_close = g['close'].shift(-5).where(valid_close.shift(-5, fill_value=False))
        ret = exit_close.div(entry).sub(1)
        result = pd.DataFrame(dict(market=market, code=code, name=g['name'],
            eligible=available, entry_date=next_dates, exit_date=exits,
            entry_open=entry, exit_close=exit_close, forward_return=ret), index=calendar)
        for pattern, mask in masks.items():
            result[pattern] = mask & available
        result['phase'] = 'warmup'
        result.loc[result.index >= research_dates[0], 'phase'] = 'research'
        result.loc[result.index >= holdout_start, 'phase'] = 'holdout'
        result.loc[(result['phase']=='research') & result['exit_date'].ge(holdout_start), 'phase'] = 'embargo'
        result['outcome_status'] = 'OK'
        result.loc[result['forward_return'].isna(), 'outcome_status'] = 'MISSING_ENTRY_OR_EXIT'
        result.loc[result['exit_date'].isna(), 'outcome_status'] = 'FORWARD_WINDOW_NOT_FINISHED'
        frames.append(result.loc[present].reset_index())
    signals = pd.concat(frames, ignore_index=True)
    return signals, holdout_start


def summarize(signals):
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
    if not daily_rows:
        raise ValueError('No eligible stock dates after warmup')
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
                unavailable_hits=len(e)-len(valid),
                unfinished_hits=int(e['outcome_status'].eq('FORWARD_WINDOW_NOT_FINISHED').sum()) if len(e) else 0,
                missing_endpoint_hits=int(e['outcome_status'].eq('MISSING_ENTRY_OR_EXIT').sum()) if len(e) else 0))
    return events, daily, pd.DataFrame(summary)


def combine(frames):
    # Construct once to retain empty/no-event patterns without pandas concat warnings.
    records = [record for frame in frames for record in frame.to_dict(orient='records')]
    return pd.DataFrame.from_records(records, columns=frames[0].columns)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'ch04_kline_batch')
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
    signals, split = build_signals(prices)
    latest_date = signals['date'].max()
    out = args.output / (latest_date.strftime('%Y%m%d')+'_nine_patterns_v1')
    out.mkdir(parents=True, exist_ok=True)
    signals.to_csv(out/'signals_all.csv', index=False, encoding='utf-8-sig')
    summaries, dailies, all_events, latest_rows = [], [], [], []
    for pattern, (direction, formula) in PATTERNS.items():
        view = signals.copy()
        view['hit'] = view[pattern]
        events, daily, summary = summarize(view)
        for df in [events,daily,summary]:
            df.insert(0, 'pattern', pattern)
            df.insert(1, 'direction', direction)
        # Bearish: a negative long-return difference supports the stated direction.
        # This is directional association, not a short-selling return.
        sign = 1 if direction=='bullish' else -1 if direction=='bearish' else None
        summary['directional_excess'] = summary['event_weighted_excess']*sign if sign else None
        summary['directional_daily_difference'] = summary['date_equal_weighted_difference']*sign if sign else None
        summaries.append(summary); dailies.append(daily); all_events.append(events)
        latest = view.loc[view['date'].eq(latest_date)&view['hit'], KEYS+['name','entry_date','exit_date','outcome_status']].copy()
        latest.insert(0,'pattern',pattern); latest.insert(1,'direction',direction)
        latest_rows.append(latest)
        print(f'{pattern}: completed', flush=True)
    summary = combine(summaries)
    for name,df in [('summary',summary),('daily',combine(dailies)),
        ('events',combine(all_events)),('latest_hits',combine(latest_rows))]:
        df.to_csv(out/(name+'.csv'),index=False,encoding='utf-8-sig')
    # Jaccard on identical eligible stock-date universe; includes all exploratory dates.
    scope=signals.loc[signals['eligible'] & signals['phase'].isin(['research','holdout'])]
    overlaps=[]
    names=list(PATTERNS)
    for i,a in enumerate(names):
        for b in names[i+1:]:
            both=int((scope[a]&scope[b]).sum()); union=int((scope[a]|scope[b]).sum())
            overlaps.append(dict(pattern_a=a,pattern_b=b,hits_a=int(scope[a].sum()),hits_b=int(scope[b].sum()),
                simultaneous_hits=both,jaccard=both/union if union else None))
    pd.DataFrame(overlaps).to_csv(out/'overlap.csv',index=False,encoding='utf-8-sig')
    definitions=pd.DataFrame([dict(pattern=k,direction=v[0],definition=v[1]) for k,v in PATTERNS.items()])
    definitions.to_csv(out/'pattern_definitions.csv',index=False,encoding='utf-8-sig')
    settings=dict(version='nine_patterns_v1',horizon=5,split_date=split.date().isoformat(),
        purpose='EXPLORATORY ONLY: both periods already exposed; neither is independent confirmation for newly selected patterns',
        universe='60 consecutive traded valid closes, valid current and previous OHLC; no current-stock-list filtering',
        returns='close(t+5)/open(t+1)-1; market-session offsets; same-date same-market non-hit controls',
        split='70/30 post-warmup dates; research returns crossing split embargoed',
        weights='unchanged', definitions=definitions.to_dict(orient='records'),
        caveats=['Unadjusted prices and no trading costs/fill/price-limit checks',
          'Multiple testing: no significance or winning-pattern claim',
          'Overlapping returns and repeated stocks are dependent',
          'Neutral doji has no preassigned predictive direction',
          'Bearish directional metrics reverse sign only; not short-sale P&L',
          'Sample universe and signal counts vary by pattern',
          'Missing endpoints and unfinished windows are separately reported'])
    (out/'settings.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    print('All results EXPLORATORY; split date:',split.date().isoformat())
    print('Returns are decimals: 0.01 = 1%; bearish directional excess reverses sign; doji is neutral.')
    print(summary[['pattern','phase','market','usable_hits','comparable_signal_dates',
        'mean_hit_return','event_weighted_excess','directional_excess','date_equal_weighted_difference',
        'unfinished_hits','missing_endpoint_hits']].to_string(index=False,float_format=lambda x:f'{x:.6f}'))
    print('Output:',out.resolve())
    print('DONE: SQL read only; factor weights and rankings unchanged.')


if __name__ == '__main__':
    main()
