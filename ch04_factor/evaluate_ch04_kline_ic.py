r"""Nine-pattern Rank IC plus matched-universe comparison with existing factors.
Place beside evaluate_ch04_klines.py. Run: py .\ch04_factor\evaluate_ch04_kline_ic.py
Reads SQL only. Reuses the exact batch pattern definitions and return calculation.
"""
import argparse
import json
import math
import os
from pathlib import Path

import pandas as pd
import pyodbc
from evaluate_ch04_klines import PATTERNS, build_signals, read_sql

KEYS = ['date', 'market', 'code']
FACTORS = ['rsi14', 'inst_net_lots', 'score_rsi', 'score_inst', 'score_ma', 'total_score']


def analyze_ic(signals, factors, min_stocks=30):
    signals, factors = signals.copy(), factors.copy()
    for df in (signals, factors):
        df['date'] = pd.to_datetime(df['date'])
        if df.duplicated(KEYS).any():
            raise ValueError('Duplicate input keys')
    for col in ['eligible'] + list(PATTERNS):
        if not signals[col].isin([True, False]).all():
            raise ValueError(f'{col}: expected booleans')
    for col in FACTORS:
        factors[col] = pd.to_numeric(factors[col], errors='raise')
    factors = factors[KEYS+FACTORS+['eligible']].rename(columns={'eligible':'factor_eligible'})
    merged = signals.merge(factors, on=KEYS, how='left', validate='one_to_one')
    rows = []
    for scope in ['kline_history', 'matched_three_factors']:
        metrics = list(PATTERNS) + (FACTORS if scope == 'matched_three_factors' else [])
        for market in ['ALL', 'TWSE', 'TPEX']:
            universe = merged if market=='ALL' else merged.loc[merged['market'].eq(market)]
            universe = universe.loc[universe['phase'].isin(['research','holdout'])]
            for (phase, day), g in universe.groupby(['phase','date'], sort=True):
                candidate = g['eligible'].eq(True)
                if scope=='matched_three_factors':
                    candidate &= g['factor_eligible'].eq(True) & g[FACTORS].notna().all(axis=1)
                paired = candidate & g['outcome_status'].eq('OK') & g['forward_return'].notna()
                y = g.loc[paired, 'forward_return']
                for metric in metrics:
                    x = g.loc[paired, metric].astype(float)
                    direction = PATTERNS[metric][0] if metric in PATTERNS else 'higher_score_higher_return'
                    reason = ''
                    if not candidate.any():
                        reason = 'NO_CANDIDATES'
                    elif g.loc[candidate,'outcome_status'].eq('FORWARD_WINDOW_NOT_FINISHED').all():
                        reason = 'FORWARD_WINDOW_NOT_FINISHED'
                    elif len(y)<min_stocks:
                        reason = 'TOO_FEW_PAIRED_STOCKS'
                    elif x.nunique()<2:
                        reason = 'CONSTANT_SIGNAL_OR_FACTOR'
                    elif y.nunique()<2:
                        reason = 'CONSTANT_RETURN'
                    ic = None if reason else x.rank(method='average').corr(y.rank(method='average'))
                    if ic is not None and not math.isfinite(ic):
                        ic,reason = None,'UNDEFINED_CORRELATION'
                    signed = None if ic is None or direction=='neutral' else (-ic if direction=='bearish' else ic)
                    rows.append(dict(scope=scope,phase=phase,date=day.date().isoformat(),market=market,
                        metric=metric,direction=direction,candidate_stocks=int(candidate.sum()),
                        paired_stocks=len(y),hits_in_pairs=int(x.sum()) if metric in PATTERNS else None,
                        unfinished_stocks=int((candidate & g['outcome_status'].eq('FORWARD_WINDOW_NOT_FINISHED')).sum()),
                        missing_endpoint_stocks=int((candidate & g['outcome_status'].eq('MISSING_ENTRY_OR_EXIT')).sum()),
                        rank_ic=ic,directional_rank_ic=signed,skip_reason=reason))
    daily = pd.DataFrame(rows)
    if daily.empty:
        raise ValueError('No post-warmup observations')
    summaries = []
    for (scope, market, metric), group in daily.groupby(['scope','market','metric'],sort=True):
        for phase in ['combined','research','holdout']:
            part = group if phase=='combined' else group.loc[group['phase'].eq(phase)]
            usable = part.dropna(subset=['rank_ic'])
            signed = usable['directional_rank_ic'].dropna()
            summaries.append(dict(scope=scope,market=market,metric=metric,phase=phase,
                available_dates=len(part),valid_dates=len(usable),
                first_date=usable['date'].min() if len(usable) else '',
                last_date=usable['date'].max() if len(usable) else '',
                mean_rank_ic=usable['rank_ic'].mean(),mean_directional_ic=signed.mean(),
                positive_direction_date_fraction=signed.gt(0).mean() if len(signed) else None,
                mean_paired_stocks=usable['paired_stocks'].mean(),
                candidate_dates_no_variation=int(part['skip_reason'].eq('CONSTANT_SIGNAL_OR_FACTOR').sum()),
                unfinished_dates=int(part['skip_reason'].eq('FORWARD_WINDOW_NOT_FINISHED').sum())))
    return daily, pd.DataFrame(summaries)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version')
    ap.add_argument('--min-stocks',type=int,default=30)
    ap.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'ch04_kline_ic')
    args=ap.parse_args()
    if args.min_stocks<3:
        raise ValueError('min-stocks must be >=3')
    driver=os.getenv('MINIQUANT_SQL_DRIVER') or next((d for d in [
        'ODBC Driver 18 for SQL Server','ODBC Driver 17 for SQL Server'] if d in pyodbc.drivers()),None)
    if not driver:
        raise ValueError('ODBC Driver 17/18 required')
    server=os.getenv('MINIQUANT_SQL_SERVER',r'localhost\SQLEXPRESS')
    database=os.getenv('MINIQUANT_SQL_DATABASE','MiniQuantDemo')
    conn=pyodbc.connect(f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;',timeout=15)
    try:
        cur=conn.cursor()
        versions=read_sql(cur,'SELECT version_id FROM dbo.ch04_versions')
        if args.version:
            versions=versions.loc[versions['version_id'].eq(args.version)]
        if len(versions)!=1:
            raise ValueError('Use --version to select exactly one version: '+', '.join(versions['version_id']))
        version=versions.iloc[0]['version_id']
        cur.execute('SELECT [date],market,code,eligible,'+','.join(FACTORS)+' FROM dbo.ch04_factors WHERE version_id=?',version)
        cols=[c[0] for c in cur.description]
        factors=pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()],columns=cols)
        prices=read_sql(cur,'SELECT [date],market,code,name,[open],high,low,[close],volume_shares,status FROM dbo.ch04_prices')
    finally:
        conn.close()
    print(f'Read SQL: prices={len(prices):,}; factors={len(factors):,}',flush=True)
    signals,split=build_signals(prices)
    daily,summary=analyze_ic(signals,factors,args.min_stocks)
    out=args.output/(signals['date'].max().strftime('%Y%m%d')+'_'+version[:12])
    out.mkdir(parents=True,exist_ok=True)
    daily.to_csv(out/'kline_ic_daily.csv',index=False,encoding='utf-8-sig')
    summary.to_csv(out/'kline_ic_summary.csv',index=False,encoding='utf-8-sig')
    settings=dict(version_id=version,horizon=5,return_definition='close(t+5)/open(t+1)-1',
        definitions=PATTERNS,min_stocks=args.min_stocks,split_date=split.date().isoformat(),
        scope='kline_history uses all eligible pattern observations; matched_three_factors requires all three factors and identical paired stocks per date',
        averaging='average-rank Spearman per day; equal-weight mean over defined IC dates; no-variation days excluded, never zero-filled',
        caveats=['All results exploratory; periods already exposed',
          'Binary signals produce ties; rare patterns may have fewer defined IC dates',
          'Identical paired stock samples do not guarantee identical valid IC dates; inspect daily skip reasons',
          'Directional IC negates bearish IC; neutral doji has no directional IC',
          'ALL pools markets; TWSE/TPEX tables allow separate checks',
          'Unadjusted prices, overlapping returns, no costs or execution guarantees',
          'Matched total-score history is only as long as institutional coverage',
          'Standalone IC does not prove incremental value or determine weights'])
    (out/'settings.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    print('IC: next-session open -> signal date +5 close; exploratory only.')
    print(summary.loc[summary['market'].eq('ALL')&summary['phase'].eq('combined'),
        ['scope','metric','valid_dates','mean_rank_ic','mean_directional_ic','positive_direction_date_fraction']].to_string(index=False,float_format=lambda x:f'{x:.6f}'))
    print('Output:',out.resolve())
    print('DONE: SQL read only. No factor/weight changes.')


if __name__=='__main__':
    main()
