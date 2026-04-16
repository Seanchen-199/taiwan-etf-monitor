"""
台股多因子模型 - 資料抓取模組 v4
涵蓋核心因子與次要因子的完整資料抓取
"""
import json, time, datetime, subprocess, sys, os

def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

print('安裝必要套件...')
for pkg in ['yfinance', 'requests', 'pandas', 'numpy']:
    install(pkg)

import requests
import yfinance as yf
import pandas as pd
import numpy as np

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.twse.com.tw/'})

def safe_float(v):
    try: return float(v)
    except: return None

def pn(s):
    try: return int(str(s).replace(',','').replace(' ','').replace('+',''))
    except: return 0

# ══════════════════════════════════════════════════
# 1. Yahoo Finance — 核心市場指數
# ══════════════════════════════════════════════════
def fetch_yahoo_core(days=60):
    """
    抓取核心因子歷史序列（60 個交易日）
    回傳 dict[symbol] = pd.Series(index=date, values=close)
    """
    print('→ Yahoo Finance 核心指數...')
    symbols = {
        '^SOX':      'SOX',        # 費半
        '^TNX':      'US10Y',      # 美10年債殖利率
        '^TWII':     'TWII',       # 台灣加權
        'DX-Y.NYB':  'DXY',        # 美元指數
        'USDTWD=X':  'USDTWD',     # 台幣匯率
        '0050.TW':   'ETF0050',    # 0050
        '006208.TW': 'ETF006208',  # 006208
        '^VIX':      'VIX',        # 恐慌指數
        '^IXIC':     'NASDAQ',     # 那斯達克
    }
    series = {}
    latest = {}
    for sym, key in symbols.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period='90d')
            if hist.empty:
                print(f'   [警告] {sym} 無資料')
                continue
            s = hist['Close'].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            series[key] = s.tail(days)
            latest[key] = {
                'price':      round(float(s.iloc[-1]), 4),
                'change_pct': round((float(s.iloc[-1]) - float(s.iloc[-2])) / float(s.iloc[-2]) * 100, 2) if len(s) >= 2 else 0,
                'prev':       round(float(s.iloc[-2]), 4) if len(s) >= 2 else None,
            }
            print(f'   {key}: {latest[key]["price"]} ({latest[key]["change_pct"]:+.2f}%)')
            time.sleep(0.4)
        except Exception as e:
            print(f'   [錯誤] {sym}: {e}')
    return series, latest

# ══════════════════════════════════════════════════
# 2. TWSE — 三大法人 + 融資融券（歷史序列）
# ══════════════════════════════════════════════════
def fetch_twse_series(n_days=60):
    """抓最近 n_days 個交易日的三大法人與融資資料"""
    print('→ TWSE 三大法人 + 融資序列...')
    today = datetime.date.today()
    records = []

    checked = 0
    d = today
    while len(records) < n_days and checked < 120:
        checked += 1
        if d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
            continue
        date_str = d.strftime('%Y%m%d')

        # 三大法人
        try:
            r = SESSION.get(
                f'https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALL',
                timeout=12
            )
            data = r.json()
            if data.get('stat') == 'OK' and data.get('data'):
                last = data['data'][-1]
                foreign_net = pn(last[4])
                invest_net  = pn(last[7])
                dealer_net  = pn(last[10])
            else:
                d -= datetime.timedelta(days=1)
                continue
        except:
            d -= datetime.timedelta(days=1)
            continue

        # 融資融券
        margin_bal = None
        try:
            r2 = SESSION.get(
                f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_str}&selectType=CS',
                timeout=12
            )
            d2 = r2.json()
            if d2.get('stat') == 'OK':
                tables = d2.get('tables', [])
                if tables:
                    rows = tables[0].get('data', [])
                    if rows:
                        margin_bal = pn(rows[-1][1])
        except:
            pass

        records.append({
            'date':         d.strftime('%Y-%m-%d'),
            'foreign_net':  foreign_net,
            'invest_net':   invest_net,
            'dealer_net':   dealer_net,
            'margin_bal':   margin_bal,
        })
        d -= datetime.timedelta(days=1)
        time.sleep(0.3)

    print(f'   取得 {len(records)} 個交易日的法人資料')
    return pd.DataFrame(records).set_index('date').sort_index() if records else pd.DataFrame()

# ══════════════════════════════════════════════════
# 3. TAIFEX — 期貨選擇權籌碼（歷史序列）
# ══════════════════════════════════════════════════
def fetch_taifex_series(n_days=60):
    """
    抓取 TAIFEX 籌碼資料：
    - 外資期貨未平倉（TXF）
    - 外資選擇權部位（TXO Put/Call）
    - 前五大/十大交易人未平倉（大台TX）
    """
    print('→ TAIFEX 期貨選擇權籌碼序列...')
    today = datetime.date.today()
    futures_records = []
    option_records  = []
    large_records   = []

    checked, d = 0, today
    while (len(futures_records) < n_days) and checked < 120:
        checked += 1
        if d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
            continue
        date_str = d.strftime('%Y/%m/%d')

        # ① 外資期貨未平倉
        try:
            r = requests.post(
                'https://www.taifex.com.tw/cht/3/futContractsDateDown',
                data={'queryStartDate': date_str, 'queryEndDate': date_str, 'commodityId': 'TXF'},
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.taifex.com.tw/'},
                timeout=12
            )
            net_long = None
            for line in r.text.strip().split('\n'):
                if '外資' in line:
                    cols = [c.strip().strip('"') for c in line.split(',')]
                    nums = []
                    for c in cols:
                        try: nums.append(int(c.replace(',','')))
                        except: pass
                    if len(nums) >= 5:
                        net_long = nums[4]
                        break
            if net_long is not None:
                futures_records.append({'date': d.strftime('%Y-%m-%d'), 'futures_foreign_net': net_long})
        except Exception as e:
            print(f'   [期貨警告] {e}')

        # ② 外資選擇權（Put/Call 淨部位）
        try:
            r2 = requests.post(
                'https://www.taifex.com.tw/cht/3/callsAndPutsDateDown',
                data={'queryStartDate': date_str, 'queryEndDate': date_str, 'commodityId': 'TXO'},
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.taifex.com.tw/'},
                timeout=12
            )
            call_net = put_net = None
            for line in r2.text.strip().split('\n'):
                if '外資' in line and 'Call' in line:
                    cols = [c.strip().strip('"') for c in line.split(',')]
                    nums = [int(c.replace(',','')) for c in cols if c.replace(',','').lstrip('-').isdigit()]
                    if len(nums) >= 3: call_net = nums[2]
                elif '外資' in line and 'Put' in line:
                    cols = [c.strip().strip('"') for c in line.split(',')]
                    nums = [int(c.replace(',','')) for c in cols if c.replace(',','').lstrip('-').isdigit()]
                    if len(nums) >= 3: put_net = nums[2]
            if call_net is not None or put_net is not None:
                option_records.append({
                    'date':          d.strftime('%Y-%m-%d'),
                    'option_call_net': call_net or 0,
                    'option_put_net':  put_net  or 0,
                    'pc_ratio':        abs(put_net / call_net) if call_net and call_net != 0 else None,
                })
        except Exception as e:
            print(f'   [選擇權警告] {e}')

        # ③ 前五大/十大交易人未平倉
        try:
            r3 = requests.post(
                'https://www.taifex.com.tw/cht/3/largeTraderFutDown',
                data={'queryStartDate': date_str, 'queryEndDate': date_str, 'commodityId': 'TX'},
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.taifex.com.tw/'},
                timeout=12
            )
            top5_long = top5_short = top10_long = top10_short = None
            lines = r3.text.strip().split('\n')
            for i, line in enumerate(lines):
                cols = [c.strip().strip('"') for c in line.split(',')]
                nums = [int(c.replace(',','')) for c in cols if c.replace(',','').lstrip('-').isdigit()]
                if len(nums) >= 4:
                    if top5_long is None:
                        top5_long, top5_short = nums[0], nums[1]
                    elif top10_long is None:
                        top10_long, top10_short = nums[0], nums[1]
            if top5_long is not None:
                large_records.append({
                    'date':         d.strftime('%Y-%m-%d'),
                    'top5_long':    top5_long,
                    'top5_short':   top5_short,
                    'top5_net':     top5_long - top5_short,
                    'top10_long':   top10_long or 0,
                    'top10_short':  top10_short or 0,
                    'top10_net':    (top10_long or 0) - (top10_short or 0),
                })
        except Exception as e:
            print(f'   [大戶警告] {e}')

        d -= datetime.timedelta(days=1)
        time.sleep(0.4)

    print(f'   期貨：{len(futures_records)} 日，選擇權：{len(option_records)} 日，大戶：{len(large_records)} 日')

    def to_df(records):
        if not records: return pd.DataFrame()
        return pd.DataFrame(records).set_index('date').sort_index()

    return to_df(futures_records), to_df(option_records), to_df(large_records)

# ══════════════════════════════════════════════════
# 主程式：整合所有資料並輸出 raw_data.json
# ══════════════════════════════════════════════════
def main():
    print('='*55)
    print('台股多因子模型 - 資料抓取 v4')
    print(f'時間：{datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")}')
    print('='*55)

    # 抓取資料
    yahoo_series, yahoo_latest = fetch_yahoo_core(days=60)
    twse_df   = fetch_twse_series(n_days=60)
    fut_df, opt_df, large_df = fetch_taifex_series(n_days=60)

    # 整合所有序列成一個大 DataFrame
    frames = {}
    for key, s in yahoo_series.items():
        frames[key] = s.rename(key)

    if not twse_df.empty:
        for col in twse_df.columns:
            frames[col] = twse_df[col]

    if not fut_df.empty:
        for col in fut_df.columns:
            frames[col] = fut_df[col]

    if not opt_df.empty:
        for col in opt_df.columns:
            frames[col] = opt_df[col]

    if not large_df.empty:
        for col in large_df.columns:
            frames[col] = large_df[col]

    # 合併
    if frames:
        combined = pd.DataFrame(frames)
        combined.index = combined.index.astype(str)

        # 序列轉為 dict（供 model.py 使用）
        series_dict = {}
        for col in combined.columns:
            series_dict[col] = combined[col].dropna().to_dict()
    else:
        series_dict = {}

    # 組裝輸出
    output = {
        'updated_at':    datetime.datetime.now().strftime('%Y/%m/%d %H:%M'),
        'updated_ts':    int(time.time()),
        'latest':        yahoo_latest,
        'series':        series_dict,
        'twse_latest': {
            'foreign_net': int(twse_df['foreign_net'].iloc[-1]) if not twse_df.empty and 'foreign_net' in twse_df else None,
            'invest_net':  int(twse_df['invest_net'].iloc[-1])  if not twse_df.empty and 'invest_net'  in twse_df else None,
            'margin_bal':  int(twse_df['margin_bal'].iloc[-1])  if not twse_df.empty and 'margin_bal'  in twse_df and twse_df['margin_bal'].notna().any() else None,
        },
        'taifex_latest': {
            'futures_foreign_net': int(fut_df['futures_foreign_net'].iloc[-1])   if not fut_df.empty   else None,
            'option_call_net':     int(opt_df['option_call_net'].iloc[-1])        if not opt_df.empty   else None,
            'option_put_net':      int(opt_df['option_put_net'].iloc[-1])         if not opt_df.empty   else None,
            'pc_ratio':            float(opt_df['pc_ratio'].iloc[-1])             if not opt_df.empty and 'pc_ratio' in opt_df and opt_df['pc_ratio'].notna().any() else None,
            'top5_net':            int(large_df['top5_net'].iloc[-1])             if not large_df.empty else None,
            'top10_net':           int(large_df['top10_net'].iloc[-1])            if not large_df.empty else None,
        },
    }

    with open('raw_data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print()
    print('='*55)
    print(f'完成！raw_data.json 已輸出')
    print(f'  Yahoo 序列：{len(yahoo_series)} 個指標')
    print(f'  TWSE 序列：{len(twse_df)} 日')
    print(f'  TAIFEX 期貨：{len(fut_df)} 日 / 選擇權：{len(opt_df)} 日 / 大戶：{len(large_df)} 日')
    print('='*55)

if __name__ == '__main__':
    main()
