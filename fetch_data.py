"""
台股 ETF 監控系統 - 自動資料抓取腳本 v2
修復：Yahoo Finance 改用 yfinance 套件，TAIFEX 改用 CSV API
"""

import json
import time
import datetime
import subprocess
import sys

def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

print('安裝必要套件...')
install('yfinance')
install('requests')

import requests
import yfinance as yf

def score(value, low, high, reverse=False):
    if value is None:
        return 5
    clamped = max(low, min(high, value))
    ratio = (clamped - low) / (high - low)
    result = ratio * 10
    return round(10 - result if reverse else result, 1)

def get_yahoo_data():
    print('-> 抓取 Yahoo Finance 資料（yfinance）...')
    result = {}
    symbols = {
        '0050.TW':   'etf_0050',
        '006208.TW': 'etf_006208',
        'USDTWD=X':  'usd_twd',
        '^VIX':      'vix',
        '^SOX':      'sox',
        '^IXIC':     'nasdaq',
        '^TWII':     'twii',
    }
    for sym, key in symbols.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period='5d')
            if hist.empty:
                print(f'   [警告] {sym} 無資料')
                continue
            price = float(hist['Close'].iloc[-1])
            prev  = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else price
            change_pct = round((price - prev) / prev * 100, 2)

            if key == 'usd_twd':
                result['usd_twd']       = round(price, 3)
                result['twd_trend']     = '升值' if change_pct < 0 else '貶值'
                result['score_usd_twd'] = score(price, 30.0, 34.0, reverse=True)
                print(f'   USD/TWD: {price:.3f}')
            elif key == 'vix':
                result['vix'] = round(price, 2)
                if price > 40:   vs, sc = '極度恐慌（逢低機會）', 8
                elif price > 30: vs, sc = '恐慌', 3
                elif price > 25: vs, sc = '警戒', 4
                elif price > 20: vs, sc = '正常偏高', 5
                elif price > 15: vs, sc = '平穩', 7
                else:            vs, sc = '極度平靜', 9
                result['vix_status']  = vs
                result['score_vix']   = sc
                print(f'   VIX: {price:.2f}（{vs}）')
            elif key == 'sox':
                result['sox']        = round(price, 2)
                result['sox_change'] = change_pct
                result['score_sox']  = score(change_pct, -5, 5)
                print(f'   SOX: {price:,.2f}（{change_pct:+.2f}%）')
            elif key == 'nasdaq':
                result['nasdaq']        = round(price, 2)
                result['nasdaq_change'] = change_pct
                print(f'   NASDAQ: {price:,.2f}（{change_pct:+.2f}%）')
            elif key == 'twii':
                result['twii']        = round(price, 2)
                result['twii_change'] = change_pct
                print(f'   TWII: {price:,.2f}（{change_pct:+.2f}%）')
            elif key == 'etf_0050':
                result['etf_0050_price']  = round(price, 2)
                result['etf_0050_change'] = change_pct
                print(f'   0050: NT${price:.2f}（{change_pct:+.2f}%）')
            elif key == 'etf_006208':
                result['etf_006208_price']  = round(price, 2)
                result['etf_006208_change'] = change_pct
                print(f'   006208: NT${price:.2f}（{change_pct:+.2f}%）')
            time.sleep(0.5)
        except Exception as e:
            print(f'   [錯誤] {sym}: {e}')
    print(f'   Yahoo 完成，取得 {len(result)} 筆')
    return result

def get_twse_institutional():
    print('-> 抓取 TWSE 三大法人資料...')
    today = datetime.date.today()
    result = {}
    for days_back in range(7):
        date = today - datetime.timedelta(days=days_back)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime('%Y%m%d')
        url = f'https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALL'
        try:
            resp = requests.get(url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://www.twse.com.tw/'
            })
            data = resp.json()
            if data.get('stat') == 'OK' and data.get('data'):
                rows = data['data']
                last = rows[-1]
                def pn(s):
                    try: return int(str(s).replace(',','').replace(' ',''))
                    except: return 0
                foreign_net = pn(last[4])
                invest_net  = pn(last[7])
                dealer_net  = pn(last[10])
                result['date_institutional'] = date.strftime('%Y/%m/%d')
                result['foreign_net_buy']    = foreign_net
                result['invest_net_buy']     = invest_net
                result['dealer_net_buy']     = dealer_net
                result['score_foreign'] = score(foreign_net, -20_000_000, 20_000_000)
                result['score_invest']  = score(invest_net,  -3_000_000,   3_000_000)
                print(f'   外資：{foreign_net/100000:.1f} 億，投信：{invest_net/100000:.1f} 億')
                break
        except Exception as e:
            print(f'   [警告] {e}')
        time.sleep(0.5)
    return result

def get_taifex_futures():
    print('-> 抓取期交所外資期貨資料...')
    today = datetime.date.today()
    result = {}
    for days_back in range(7):
        date = today - datetime.timedelta(days=days_back)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime('%Y/%m/%d')
        url = 'https://www.taifex.com.tw/cht/3/futContractsDateDown'
        try:
            resp = requests.post(url, data={
                'queryStartDate': date_str,
                'queryEndDate':   date_str,
                'commodityId':    'TXF',
            }, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://www.taifex.com.tw/'
            }, timeout=15)
            lines = resp.text.strip().split('\n')
            for line in lines:
                if '外資' in line or 'Foreign' in line:
                    cols = [c.strip().strip('"') for c in line.split(',')]
                    nums = []
                    for c in cols:
                        try: nums.append(int(c.replace(',','')))
                        except: pass
                    if len(nums) >= 5:
                        net = nums[4]
                        result['date_futures']        = date.strftime('%Y/%m/%d')
                        result['futures_foreign_net'] = net
                        result['score_futures']       = score(net, -50000, 50000)
                        print(f'   外資期貨淨多單：{net:,} 口')
                        return result
        except Exception as e:
            print(f'   [警告] TAIFEX: {e}')
        time.sleep(0.5)
    print('   [警告] 期交所資料抓取失敗，使用中性預設值')
    result['score_futures'] = 5
    return result

def main():
    print('=' * 50)
    print('台股 ETF 監控系統 - 資料抓取 v2')
    print(f'執行時間：{datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")}')
    print('=' * 50)

    output = {
        'updated_at': datetime.datetime.now().strftime('%Y/%m/%d %H:%M'),
        'updated_ts': int(time.time()),
        'source': {
            'yahoo':  'Yahoo Finance (yfinance)',
            'twse':   '台灣證交所',
            'taifex': '台灣期交所',
        }
    }
    output.update(get_yahoo_data())
    output.update(get_twse_institutional())
    output.update(get_taifex_futures())

    auto_scores = {}
    mapping = {
        'score_usd_twd': 's13',
        'score_vix':     's15',
        'score_sox':     's16',
        'score_foreign': 's2',
        'score_invest':  's3',
        'score_futures': 's5',
    }
    for src_key, ind_id in mapping.items():
        if src_key in output:
            auto_scores[ind_id] = output[src_key]

    output['auto_scores']      = auto_scores
    output['auto_score_count'] = len(auto_scores)

    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print('=' * 50)
    print(f'完成！自動更新 {len(auto_scores)} 個指標')
    labels = {'s2':'外資買超','s3':'投信買超','s5':'外資期貨',
              's13':'台幣匯率','s15':'VIX','s16':'費半走勢'}
    for k, v in auto_scores.items():
        print(f'  {labels.get(k,k)}: {v}/10')
    print('=' * 50)

if __name__ == '__main__':
    main()
