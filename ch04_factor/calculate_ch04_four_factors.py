r"""Experimental four-factor ranking and paired IC; SQL read only.
Place beside evaluate_ch04_klines.py.
Run: py .\ch04_factor\calculate_ch04_four_factors.py
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pyodbc
from evaluate_ch04_klines import PATTERNS, build_signals, read_sql

KEYS = ['date','market','code']
WEIGHTS = dict(rsi=.36, inst=.315, ma=.225, kline=.10)


def calculate(factors, signals):
    f,s=factors.copy(),signals.copy()
    for df in [f,s]:
        df['date']=pd.to_datetime(df['date'])
        if df.duplicated(KEYS).any():
            raise ValueError('Duplicate input keys')
    for col in ['total_score','score_rsi','score_inst','score_ma']:
        f[col]=pd.to_numeric(f[col],errors='raise')
    s=s[KEYS+['eligible','forward_return','outcome_status','phase']+list(PATTERNS)].rename(columns={'eligible':'kline_eligible'})
    data=f.merge(s,on=KEYS,how='left',validate='one_to_one')
    bullish=[p for p,v in PATTERNS.items() if v[0]=='bullish']
    bearish=[p for p,v in PATTERNS.items() if v[0]=='bearish']
    masks=data[list(PATTERNS)].fillna(False).astype(bool)
    data['bullish_count']=masks[bullish].sum(axis=1)
    data['bearish_count']=masks[bearish].sum(axis=1)
    data['bullish_patterns']=masks[bullish].apply(lambda r:';'.join(r.index[r]),axis=1)
    data['bearish_patterns']=masks[bearish].apply(lambda r:';'.join(r.index[r]),axis=1)
    data['score_kline']=5.
    data.loc[data.bullish_count.gt(0)&data.bearish_count.eq(0),'score_kline']=10.
    data.loc[data.bearish_count.gt(0)&data.bullish_count.eq(0),'score_kline']=0.
    # No unknown candle is silently treated as neutral.
    data.loc[~data['kline_eligible'].eq(True),'score_kline']=float('nan')
    data['four_eligible']=(data['eligible'].eq(True)&data['kline_eligible'].eq(True)
        &data[['total_score','score_rsi','score_inst','score_ma']].notna().all(axis=1))
    data['exclusion_four']=''
    data.loc[~data['eligible'].eq(True),'exclusion_four']='BASE_THREE_FACTORS_UNAVAILABLE'
    data.loc[~data['kline_eligible'].eq(True),'exclusion_four']+=';KLINE_UNAVAILABLE'
    data['weighted_rsi_four']=data.score_rsi*WEIGHTS['rsi']
    data['weighted_inst_four']=data.score_inst*WEIGHTS['inst']
    data['weighted_ma_four']=data.score_ma*WEIGHTS['ma']
    data['weighted_kline_four']=data.score_kline*WEIGHTS['kline']
    data['total_four']=data[['weighted_rsi_four','weighted_inst_four','weighted_ma_four','weighted_kline_four']].sum(axis=1,min_count=4).round(12).where(data.four_eligible)
    data['rank_original_three']=data['total_score'].where(data['eligible'].eq(True)).groupby(data['date']).rank(method='min',ascending=False)
    data['rank_three_common']=data['total_score'].where(data.four_eligible).groupby(data['date']).rank(method='min',ascending=False)
    data['rank_four']=data['total_four'].groupby(data['date']).rank(method='min',ascending=False)
    data['rank_improvement_common']=data['rank_three_common']-data['rank_four']
    data['total_change']=data['total_four']-data['total_score']
    return data


def paired_ic(data,min_stocks=30):
    rows=[]
    for market in ['ALL','TWSE','TPEX']:
        scope=data if market=='ALL' else data.loc[data.market.eq(market)]
        for day,g in scope.groupby('date',sort=True):
            candidates=g.loc[g.four_eligible]
            if candidates.empty:
                continue
            paired=candidates.loc[candidates.outcome_status.eq('OK')&candidates.forward_return.notna()]
            reason=''
            if candidates.outcome_status.eq('FORWARD_WINDOW_NOT_FINISHED').all():
                reason='FORWARD_WINDOW_NOT_FINISHED'
            elif len(paired)<min_stocks:
                reason='TOO_FEW_STOCKS'
            elif paired.forward_return.nunique()<2:
                reason='CONSTANT_RETURN'
            elif paired.total_score.nunique()<2 or paired.total_four.nunique()<2:
                reason='CONSTANT_SCORE_IN_ONE_MODEL'
            old=new=None
            if not reason:
                y=paired.forward_return.rank(method='average')
                old=paired.total_score.rank(method='average').corr(y)
                new=paired.total_four.rank(method='average').corr(y)
            rows.append(dict(date=day.date().isoformat(),market=market,candidates=len(candidates),
                paired_stocks=len(paired),ic_three=old,ic_four=new,
                ic_difference=None if reason else new-old,skip_reason=reason))
    daily=pd.DataFrame(rows,columns=['date','market','candidates','paired_stocks','ic_three','ic_four','ic_difference','skip_reason'])
    summaries=[]
    for market in ['ALL','TWSE','TPEX']:
        valid=daily.loc[daily.market.eq(market)].dropna(subset=['ic_three','ic_four'])
        summaries.append(dict(market=market,valid_dates=len(valid),
            first_date=valid.date.min() if len(valid) else '',last_date=valid.date.max() if len(valid) else '',
            mean_ic_three=valid.ic_three.mean(),mean_ic_four=valid.ic_four.mean(),
            mean_ic_difference=valid.ic_difference.mean(),
            fraction_dates_improved=valid.ic_difference.gt(0).mean() if len(valid) else None))
    return daily,pd.DataFrame(summaries)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version')
    ap.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'ch04_four_factors')
    args=ap.parse_args()
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
            versions=versions.loc[versions.version_id.eq(args.version)]
        if len(versions)!=1:
            raise ValueError('Select one --version: '+', '.join(versions.version_id))
        version=versions.iloc[0].version_id
        cur.execute('SELECT * FROM dbo.ch04_factors WHERE version_id=?',version)
        cols=[c[0] for c in cur.description]
        factors=pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()],columns=cols)
        prices=read_sql(cur,'SELECT [date],market,code,name,[open],high,low,[close],volume_shares,status FROM dbo.ch04_prices')
    finally:
        conn.close()
    if factors.empty:
        raise ValueError('No factors for version')
    print(f'Read SQL: prices={len(prices):,}; factors={len(factors):,}',flush=True)
    signals,split=build_signals(prices)
    if pd.to_datetime(factors.date).max()!=signals.date.max():
        raise ValueError('Price/factor latest dates differ. Recalculate/import factors first.')
    data=calculate(factors,signals)
    daily,summary=paired_ic(data)
    latest=data.loc[data.date.eq(data.date.max())].copy()
    ranking=latest.loc[latest.four_eligible].sort_values(['rank_four','market','code'])
    settings=dict(experiment='four_factors_v1',base_version=version,weights=WEIGHTS,
        kline_score='10 bullish-only; 0 bearish-only; 5 neither or conflicting; doji neutral; same-direction patterns do not stack',
        definitions=PATTERNS,pattern_code_sha256=hashlib.sha256(Path(__file__).with_name('evaluate_ch04_klines.py').read_bytes()).hexdigest(),
        eligibility='base three-factor eligible AND batch-kline eligible; unavailable signal is excluded, not neutral',
        tie_rule='competition rank: same rounded total gives same rank; market/code inside ties for display only',
        ic='paired same-date same-stock 5-session next-open to close returns; same valid IC dates in both models; minimum 30 stocks',
        interpretation='exploratory integration test; unadjusted prices; no cost/fill/shorting assumptions; no independent confirmation; overlapping returns and short institutional history',
        sql='read only; existing rankings and factors unchanged')
    exp=hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()[:12]
    out=args.output/(latest.date.max().strftime('%Y%m%d')+'_'+exp)
    out.mkdir(parents=True,exist_ok=True)
    for name,frame in [('ranking_four',ranking),('latest_all',latest),
        ('excluded',latest.loc[~latest.four_eligible]),('four_factor_history',data),
        ('ic_daily_comparison',daily),('ic_summary_comparison',summary)]:
        frame.to_csv(out/(name+'.csv'),index=False,encoding='utf-8-sig')
    (out/'settings.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Weights: RSI 36% / institutions 31.5% / MA 22.5% / Kline 10%')
    print(f'As of {latest.date.max().date()}: original eligible={int(latest.eligible.eq(True).sum())}; four-factor eligible={len(ranking)}')
    print('Kline score counts:',ranking.score_kline.value_counts().sort_index().to_dict())
    print('Top 20 rows (ties preserved in full CSV):')
    print(ranking.head(20)[['rank_four','rank_three_common','rank_improvement_common','market','code','name',
        'score_kline','total_score','total_four','bullish_patterns','bearish_patterns']].to_string(index=False))
    print('\nPaired IC comparison (exploratory, returns are next-open -> t+5 close):')
    print(summary.to_string(index=False,float_format=lambda x:f'{x:.6f}'))
    print('Output:',out.resolve())
    print('DONE: experimental results only; SQL and original rankings unchanged.')


if __name__=='__main__':
    main()
