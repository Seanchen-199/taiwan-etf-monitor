"""
台股 ETF 監控系統 - 自動資料抓取腳本 v3
新增：Discord Webhook 推播通知
"""

import json
import time
import datetime
import subprocess
import sys
import os

def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

print('安裝必要套件...')
install('yfinance')
install('requests')

import requests
import yfinance as yf

# ── 工具函式 ────────────────────────────────────────────────
def score(value, low, high, reverse=False):
    if value is None:
        return 5
    clamped = max(low, min(high, value))
    ratio = (clamped - low) / (high - low)
    result = ratio * 10
    return round(10 - result if reverse else result, 1)

def get_verdict(total):
    if total >= 70:   return '積極做多',  '建議積極加碼 0050／006208（+20~30%）', '🟢'
    if total >= 65:   return '多方偏強',  '建議小幅加碼（+10%），持續觀察',       '🟢'
    if total >= 45:   return '中性觀望',  '維持標準倉位，暫緩加碼',               '⚪'
    if total >= 35:   return '審慎減碼',  '建議減碼 10~20%，控制風險',            '🟡'
    return               '空方偏強',  '建議大幅減碼或暫時空手',               '🔴'

def calc_total(auto_scores, prev_scores=None):
    """用自動分數 + 前次手動分數 計算總分（自動覆蓋手動）"""
    # 預設各指標中性 5 分
    all_scores = {f's{i}': 5 for i in range(1, 23)}
    # 載入前次的完整分數（如果有）
    if prev_scores:
        all_scores.update(prev_scores)
    # 用新的自動分數覆蓋
    all_scores.update(auto_scores)

    CATEGORIES = [
        {'ids': ['s1','s2','s3','s4'],          'weight': 0.25},
        {'ids': ['s5','s6','s7','s8'],           'weight': 0.20},
        {'ids': ['s9','s10','s11','s12'],        'weight': 0.20},
        {'ids': ['s13','s14','s15','s16'],       'weight': 0.20},
        {'ids': ['s17','s18','s19'],             'weight': 0.10},
        {'ids': ['s20','s21','s22'],             'weight': 0.05},
    ]
    total = 0
    for cat in CATEGORIES:
        s = sum(all_scores.get(i, 5) for i in cat['ids'])
        total += (s / (len(cat['ids']) * 10)) * cat['weight'] * 100
    return round(total)

# ── Discord 推播 ─────────────────────────────────────────────
def send_discord(webhook_url, data, total, prev_total):
    """傳送 Discord 通知"""
    verdict, action, emoji = get_verdict(total)
    prev_verdict, _, _ = get_verdict(prev_total) if prev_total else ('--', '', '')

    # 判斷是否需要通知（評級改變 或 分數差距 >= 5）
    changed = (verdict != prev_verdict) or (abs(total - (prev_total or total)) >= 5)
    if not changed:
        print(f'   評分無重大變化（{prev_total} → {total}），略過推播')
        return

    # 顏色：多=綠、空=紅、中性=灰
    color = 0x22c97a if total >= 65 else 0xf05252 if total < 45 else 0x6b8cba

    # 組裝市場數據欄位
    fields = []
    if data.get('etf_0050_price'):
        chg = data.get('etf_0050_change', 0)
        fields.append({'name': '0050', 'value': f"NT${data['etf_0050_price']} ({chg:+.2f}%)", 'inline': True})
    if data.get('etf_006208_price'):
        chg = data.get('etf_006208_change', 0)
        fields.append({'name': '006208', 'value': f"NT${data['etf_006208_price']} ({chg:+.2f}%)", 'inline': True})
    if data.get('usd_twd'):
        fields.append({'name': '台幣匯率', 'value': f"USD/TWD {data['usd_twd']} ({data.get('twd_trend','--')})", 'inline': True})
    if data.get('vix'):
        fields.append({'name': 'VIX', 'value': f"{data['vix']} {data.get('vix_status','')}", 'inline': True})
    if data.get('sox'):
        chg = data.get('sox_change', 0)
        fields.append({'name': '費半 SOX', 'value': f"{data['sox']:,.0f} ({chg:+.2f}%)", 'inline': True})
    if data.get('foreign_net_buy') is not None:
        val = data['foreign_net_buy'] / 100000
        fields.append({'name': '外資買賣超', 'value': f"{val:+.1f} 億", 'inline': True})
    if data.get('futures_foreign_net') is not None:
        fields.append({'name': '外資期貨淨多單', 'value': f"{data['futures_foreign_net']:,} 口", 'inline': True})

    score_change = f"{prev_total} → {total}" if prev_total else str(total)
    description = (
        f"**{action}**\n\n"
        f"評分變化：{score_change} 分\n"
        f"前次狀態：{prev_verdict}　→　現在：**{verdict}**"
    )

    payload = {
        'embeds': [{
            'title': f'{emoji} 台股 ETF 監控系統 · 訊號更新',
            'description': description,
            'color': color,
            'fields': fields,
            'footer': {'text': f'更新時間：{data.get("updated_at","--")}　｜　資料來源：Yahoo Finance / TWSE / TAIFEX'},
            'thumbnail': {'url': 'https://upload.wikimedia.org/wikipedia/commons/thumb/7/72/Flag_of_the_Republic_of_China.svg/320px-Flag_of_the_Republic_of_China.svg.png'}
        }]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            print(f'   ✅ Discord 推播成功！評分：{score_change}，狀態：{verdict}')
        else:
            print(f'   ❌ Discord 推播失敗：{resp.status_code} {resp.text}')
    except Exception as e:
        print(f'   ❌ Discord 推播錯誤：{e}')

# ── Yahoo Finance ────────────────────────────────────────────
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
                print(f'   USD/TWD: {price:.3f}（{result["twd_trend"]}）')
            elif key == 'vix':
                result['vix'] = round(price, 2)
                if price > 40:   vs, sc = '極度恐慌（逢低機會）', 8
                elif price > 30: vs, sc = '恐慌', 3
                elif price > 25: vs, sc = '警戒', 4
                elif price > 20: vs, sc = '正常偏高', 5
                elif price > 15: vs, sc = '平穩', 7
                else:            vs, sc = '極度平靜', 9
                result['vix_status'] = vs
                result['score_vix']  = sc
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

# ── TWSE 三大法人 ────────────────────────────────────────────
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
                last = data['data'][-1]
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

# ── TAIFEX 外資期貨淨多單 ─────────────────────────────────────
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

# ── 主程式 ───────────────────────────────────────────────────
def main():
    print('=' * 50)
    print('台股 ETF 監控系統 - 資料抓取 v3')
    print(f'執行時間：{datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")}')
    print('=' * 50)

    # 讀取前次 data.json（取得上次評分，用於比對是否需要推播）
    prev_total  = None
    prev_scores = None
    try:
        with open('data.json', 'r', encoding='utf-8') as f:
            prev_data   = json.load(f)
            prev_total  = prev_data.get('total_score')
            prev_scores = prev_data.get('all_scores')
        print(f'前次評分：{prev_total} 分')
    except:
        print('無前次資料，首次執行')

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

    # 整合自動評分
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

    # 計算本次總分
    total = calc_total(auto_scores, prev_scores)
    output['total_score'] = total

    # 保存完整分數供下次比對
    all_scores = {f's{i}': 5 for i in range(1, 23)}
    if prev_scores:
        all_scores.update(prev_scores)
    all_scores.update(auto_scores)
    output['all_scores'] = all_scores

    # 寫出 data.json
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print('=' * 50)
    verdict, action, emoji = get_verdict(total)
    print(f'本次總分：{total} 分　{emoji} {verdict}')
    print(f'操作建議：{action}')
    print(f'自動更新指標數：{len(auto_scores)} / 22')
    print('=' * 50)

    # Discord 推播
    webhook_url = os.environ.get('DISCORD_WEBHOOK')
    if webhook_url:
        print()
        print('-> 傳送 Discord 通知...')
        send_discord(webhook_url, output, total, prev_total)
    else:
        print()
        print('（未設定 DISCORD_WEBHOOK，略過推播）')

if __name__ == '__main__':
    main()
