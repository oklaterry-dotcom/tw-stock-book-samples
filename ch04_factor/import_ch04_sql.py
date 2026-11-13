"""Import chapter 4 CSVs into SQL Server. Default: validate only; --apply writes.

Existing dbo.prices and dbo.TWSE_ALL_202611 are never modified.
Requires pandas and pyodbc. Place beside calculate_ch04_factors.py.
"""
import argparse
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path

import pandas as pd
import pyodbc

KEYS = ['date', 'market', 'code']
TEXT = {'date', 'market', 'code', 'name', 'status', 'price_status',
        'exclusion_reason', 'version_id'}
INTS = {'inst_net_shares', 'volume_shares', 'foreign_net_shares', 'trust_net_shares',
        'valid_days_last60', 'contiguous_valid_days', 'rank'}


def quote(s):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', s):
        raise ValueError('Unsafe column: ' + s)
    return '[' + s + ']'


def sql_type(col):
    if col == 'date':
        return 'date'
    if col in ('market', 'code'):
        return 'nvarchar(20)'
    if col == 'version_id':
        return 'nvarchar(64)'
    if col in TEXT:
        return 'nvarchar(1000)'
    if col == 'eligible':
        return 'bit'
    return 'bigint' if col in INTS else 'float'


def load(path, required):
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')
    if not set(required) <= set(df):
        raise ValueError(f'{path}: missing {set(required) - set(df)}')
    if df.empty or df.duplicated(KEYS).any():
        raise ValueError(f'{path}: empty file or duplicate keys')
    for c in df:
        quote(c)
        if c == 'date':
            df[c] = [__import__('datetime').date.fromisoformat(v) for v in df[c]]
        elif c in TEXT:
            limit = 20 if c in ('market', 'code') else 1000
            if df[c].str.len().gt(limit).any():
                raise ValueError(f'{path}: {c} exceeds SQL length')
        elif c == 'eligible':
            if not df[c].isin(['True', 'False']).all():
                raise ValueError('Invalid eligible value')
            df[c] = df[c].eq('True')
        else:
            values = []
            for v in df[c]:
                if v == '':
                    values.append(None)
                    continue
                n = float(v)
                if not math.isfinite(n):
                    raise ValueError(f'{path}: non-finite {c}')
                if c in INTS:
                    from decimal import Decimal
                    dec = Decimal(v)
                    if dec != dec.to_integral_value():
                        raise ValueError(f'{path}: fractional {c}')
                    n = int(dec)
                values.append(n)
            df[c] = pd.Series(values, dtype=object)
    if not df['market'].isin(['TWSE', 'TPEX']).all() or df['code'].eq('').any():
        raise ValueError('Invalid market/code')
    return df


def upsert(cur, table, df, keys):
    # A schema-scoped table avoids local-temp scope/metadata issues with ODBC.
    # Creation and removal are in the caller transaction; rollback also removes it.
    stage = "dbo." + quote("ch04_stage_" + uuid.uuid4().hex)
    cols = list(df)
    definitions = ', '.join(quote(c) + ' ' + sql_type(c) +
                            (' NOT NULL' if c in keys else ' NULL') for c in cols)
    key_sql = ', '.join(map(quote, keys))
    cur.execute(f"IF OBJECT_ID('dbo.{table}', 'U') IS NULL "
                f'CREATE TABLE dbo.{table} ({definitions}, PRIMARY KEY ({key_sql}))')
    cur.execute('SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS '
                "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", table)
    if [r[0] for r in cur] != cols:
        raise ValueError(f'dbo.{table}: schema differs; no changes committed')
    cur.execute(f'CREATE TABLE {stage} ({definitions}, PRIMARY KEY ({key_sql}))')
    col_sql = ', '.join(map(quote, cols))
    insert = f'INSERT INTO {stage} ({col_sql}) VALUES ({",".join("?" for _ in cols)})'
    rows = []
    for row in df.itertuples(index=False, name=None):
        rows.append(tuple(None if pd.isna(v) else v for v in row))
        if len(rows) == 2000:
            cur.executemany(insert, rows)
            rows.clear()
    if rows:
        cur.executemany(insert, rows)
    match = ' AND '.join(f't.{quote(c)}=s.{quote(c)}' for c in keys)
    cur.execute(f'SELECT COUNT_BIG(*) FROM {stage} s JOIN dbo.{table} t ON {match}')
    existing = cur.fetchone()[0]
    assignments = ', '.join(f't.{quote(c)}=s.{quote(c)}' for c in cols if c not in keys)
    cur.execute(f'UPDATE t SET {assignments} FROM dbo.{table} t JOIN {stage} s ON {match}')
    cur.execute(f'INSERT INTO dbo.{table} ({col_sql}) SELECT '
                + ', '.join('s.' + quote(c) for c in cols)
                + f' FROM {stage} s WHERE NOT EXISTS (SELECT 1 FROM dbo.{table} t WHERE {match})')
    cur.execute(f'SELECT COUNT_BIG(*) FROM {stage} s JOIN dbo.{table} t ON {match}')
    if cur.fetchone()[0] != len(df):
        raise ValueError('Post-import count mismatch')
    cur.execute(f'DROP TABLE {stage}')
    print(f'{table}: input={len(df):,}; inserted={len(df)-existing:,}; matched/updated={existing:,}', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent / 'ch04_data' / '20260501_20261112')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    folder = args.data_dir
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8-sig'))
    if summary.get('status') != 'COMPLETE' or summary.get('probe'):
        raise ValueError('Require COMPLETE download')
    prices = load(folder / 'prices.csv', KEYS + ['close', 'volume_shares', 'status'])
    inst = load(folder / 'institutions.csv', KEYS + ['foreign_net_shares', 'trust_net_shares', 'inst_net_lots', 'status'])
    latest = max(prices['date'])
    out = folder / ('factors_' + latest.strftime('%Y%m%d'))
    settings = json.loads((out / 'calculation_settings.json').read_text(encoding='utf-8-sig'))
    if settings['as_of'] != latest.isoformat():
        raise ValueError('Factor date mismatch')
    calculator = Path(__file__).with_name('calculate_ch04_factors.py')
    metadata = json.dumps({'settings': settings,
        'calculator_sha256': hashlib.sha256(calculator.read_bytes()).hexdigest()},
        ensure_ascii=False, sort_keys=True)
    version = hashlib.sha256(metadata.encode()).hexdigest()
    factors = load(out / 'factor_history.csv', KEYS + ['eligible', 'total_score', 'rsi14', 'ma60'])
    if set(map(tuple, prices[KEYS].to_numpy())) != set(map(tuple, factors[KEYS].to_numpy())):
        raise ValueError('Price/factor key sets differ')
    eligible = factors['eligible']
    if factors.loc[eligible, 'total_score'].isna().any():
        raise ValueError('Eligible row missing total score')
    factors['rank'] = None
    factors.loc[eligible, 'rank'] = factors.loc[eligible].groupby('date')['total_score'].rank(
        method='min', ascending=False).astype(int)
    factors['version_id'] = version
    ranking = load(out / 'ranking.csv', KEYS + ['rank', 'total_score'])
    expected = factors.loc[eligible & factors['date'].eq(latest), KEYS + ['rank', 'total_score']]
    check = ranking.merge(expected, on=KEYS, how='outer', suffixes=('_csv', '_calc'), indicator=True)
    if (not check['_merge'].eq('both').all()
        or not check['rank_csv'].eq(check['rank_calc']).all()
        or not check['total_score_csv'].eq(check['total_score_calc']).all()):
        raise ValueError('Latest CSV ranking differs from history')
    print(f'Validation OK: prices={len(prices):,}; institutions={len(inst):,}; factors={len(factors):,}; latest ranked={len(ranking):,}')
    print('Version:', version)
    if not args.apply:
        print('No SQL changes. Run with --apply to import.')
        return
    driver = os.getenv('MINIQUANT_SQL_DRIVER') or next((d for d in [
        'ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server'] if d in pyodbc.drivers()), None)
    if not driver:
        raise ValueError('SQL Server ODBC Driver 17/18 required')
    server = os.getenv('MINIQUANT_SQL_SERVER', r'localhost\SQLEXPRESS')
    database = os.getenv('MINIQUANT_SQL_DATABASE', 'MiniQuantDemo')
    print(f'Target: {server} / {database}', flush=True)
    conn = pyodbc.connect(f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;', timeout=15)
    try:
        cur = conn.cursor()
        cur.execute('SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
        cur.execute("DECLARE @r int; EXEC @r=sys.sp_getapplock @Resource='ch04_import', @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=15000; IF @r < 0 THROW 50001, 'Import lock failed', 1;")
        cur.execute("IF OBJECT_ID('dbo.ch04_versions','U') IS NULL CREATE TABLE dbo.ch04_versions (version_id nvarchar(64) NOT NULL PRIMARY KEY, metadata nvarchar(max) NOT NULL)")
        cur.execute('IF NOT EXISTS (SELECT 1 FROM dbo.ch04_versions WHERE version_id=?) INSERT INTO dbo.ch04_versions VALUES (?,?)', version, version, metadata)
        upsert(cur, 'ch04_prices', prices, KEYS)
        upsert(cur, 'ch04_institutions', inst, KEYS)
        upsert(cur, 'ch04_factors', factors, KEYS + ['version_id'])
        conn.commit()
        print('COMMITTED: all tables imported. Original tables unchanged.')
    except BaseException:
        conn.rollback()
        print('ROLLED BACK: no changes committed.')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
