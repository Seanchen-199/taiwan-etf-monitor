"""
台股 ETF 監控系統 - 自動資料抓取腳本
執行後會產生 data.json，供網站前端讀取

資料來源：
- Yahoo Finance API（股價、匯率、VIX、費半、那斯達克）
- TWSE 台灣證交所（三大法人、融資融券）
- TAIFEX 台灣期交所（外資期貨淨多單）
"""

import json
import time
import datetime
import urllib.request
import urllib.parse

# ── 工具函式 ────────────────────────────────────────────────
def fetch_json(url, headers=None):
    """抓取 JSON 資料"""
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (compatible; ETF-Monitor/1.0)')
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'  [錯誤] fetch_json {url[:60]}... → {e}')
        return None

def fetch_text(url, encoding='utf-8'):
    """抓取純文字資料"""
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (compatible; ETF-Monitor/1.0)')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return raw.decode(encoding, errors='replace')
    except Exception as e:
        print(f'  [錯誤] fetch_text {url[:60]}... → {e}')
        return None

def score(value, low, high, reverse=False):
    """
    將數值線性對應到 0~10 分
    reverse=True 表示數值越高分數越低（如 VIX）
    """
    if value is None:
        return 5  # 抓不到資料給中性分
    clamped = max(low, min(high, value))
    ratio = (clamped - low) / (high - low)
    result = ratio * 10
    return round(10 - result if reverse else result, 1)

# ── Yahoo Finance 抓取 ───────────────────────────────────────
def yahoo_quote(symbols):
    """
    用 Yahoo Finance v8 API 批次抓股票/指數報價
    回傳 dict: { symbol: { price, change_pct, ... } }
    """
    joined = '%2C'.join(symbols)
    url = (
        f'https://query1.finance.yahoo.com/v8/finance/spark'
        f'?symbols={joined}&range=5d&interval=1d'
    )
    data = fetch_json(url)
    result = {}
    if not data:
        return result

    # 改用 v7 quoteSummary 批次取得最新價格
    fields = 'price'
    for sym in symbols:
        url2 = (
            f'https://query1.finance.yahoo.com/v7/finance/quote'
            f'?symbols={sym}&fields=regularMarketPrice,regularMarketChangePercent'
        )
        d = fetch_json(url2)
        if d and 'quoteResponse' in d:
            quotes = d['quoteResponse'].get('result', [])
            if quotes:
                q = quotes[0]
                result[sym] = {
                    'price': q.get('regularMarketPrice'),
                    'change_pct': q.get('regularMarketChangePercent'),
                }
        time.sleep(0.3)  # 避免被封鎖

    return result

def get_yahoo_data():
    """抓取所有 Yahoo Finance 資料"""
    print('→ 抓取 Yahoo Finance 資料...')
    symbols = [
        '0050.TW',    # 元大台灣50
        '006208.TW',  # 富邦台灣50
        'USDTWD=X',   # 美元兌台幣匯率
        '^VIX',       # VIX 恐慌指數
        '^SOX',       # 費城半導體指數
        '^IXIC',      # 那斯達克指數
        '^TWII',      # 加權指數
    ]

    quotes = yahoo_quote(symbols)

    result = {}

    # 0050 股價
    if '0050.TW' in quotes and quotes['0050.TW']['price']:
        result['etf_0050_price'] = quotes['0050.TW']['price']
        result['etf_0050_change'] = round(quotes['0050.TW']['change_pct'] or 0, 2)

    # 006208 股價
    if '006208.TW' in quotes and quotes['006208.TW']['price']:
        result['etf_006208_price'] = quotes['006208.TW']['price']
        result['etf_006208_change'] = round(quotes['006208.TW']['change_pct'] or 0, 2)

    # 台幣匯率（USDTWD，數字越大=台幣越弱）
    if 'USDTWD=X' in quotes and quotes['USDTWD=X']['price']:
        usd_twd = quotes['USDTWD=X']['price']
        result['usd_twd'] = round(usd_twd, 3)
        change = quotes['USDTWD=X']['change_pct'] or 0
        # 匯率下跌 = 台幣升值 = 正面
        # 分數：30.0~34.0 之間，越低（台幣越強）分越高
        result['score_usd_twd'] = score(usd_twd, 30.0, 34.0, reverse=True)
        result['twd_trend'] = '升值' if change < 0 else '貶值'

    # VIX 恐慌指數
    if '^VIX' in quotes and quotes['^VIX']['price']:
        vix = quotes['^VIX']['price']
        result['vix'] = round(vix, 2)
        # VIX 15以下=樂觀(高分), 35以上=極恐慌(可能反向正面)
        # 簡單線性：VIX 15~35，越低越正面
        if vix > 40:
            result['score_vix'] = 8  # 極度恐慌 → 反向加碼機會
        elif vix > 30:
            result['score_vix'] = 3
        elif vix > 25:
            result['score_vix'] = 4
        elif vix > 20:
            result['score_vix'] = 5
        elif vix > 15:
            result['score_vix'] = 7
        else:
            result['score_vix'] = 9
        result['vix_status'] = (
            '極度恐慌（逢低機會）' if vix > 40 else
            '恐慌' if vix > 30 else
            '警戒' if vix > 25 else
            '正常偏高' if vix > 20 else
            '平穩' if vix > 15 else '極度平靜'
        )

    # 費半指數
    if '^SOX' in quotes and quotes['^SOX']['price']:
        sox = quotes['^SOX']['price']
        sox_chg = quotes['^SOX']['change_pct'] or 0
        result['sox'] = round(sox, 2)
        result['sox_change'] = round(sox_chg, 2)
        # 費半日漲跌幅 → 分數：-5%以下=0分, +5%以上=10分
        result['score_sox'] = score(sox_chg, -5, 5)

    # 那斯達克
    if '^IXIC' in quotes and quotes['^IXIC']['price']:
        ixic = quotes['^IXIC']['price']
        ixic_chg = quotes['^IXIC']['change_pct'] or 0
        result['nasdaq'] = round(ixic, 2)
        result['nasdaq_change'] = round(ixic_chg, 2)

    # 台灣加權指數
    if '^TWII' in quotes and quotes['^TWII']['price']:
        twii = quotes['^TWII']['price']
        twii_chg = quotes['^TWII']['change_pct'] or 0
        result['twii'] = round(twii, 2)
        result['twii_change'] = round(twii_chg, 2)

    print(f'   完成：抓到 {len(result)} 筆資料')
    return result

# ── TWSE 三大法人 ────────────────────────────────────────────
def get_twse_institutional():
    """
    抓取三大法人買賣超資料
    來源：台灣證交所 opendata API
    """
    print('→ 抓取 TWSE 三大法人資料...')
    today = datetime.date.today()
    result = {}

    # 嘗試最近 5 個交易日（跳過假日）
    for days_back in range(5):
        date = today - datetime.timedelta(days=days_back)
        if date.weekday() >= 5:  # 跳過週末
            continue
        date_str = date.strftime('%Y%m%d')

        url = (
            f'https://www.twse.com.tw/rwd/zh/fund/T86'
            f'?response=json&date={date_str}&selectType=ALL'
        )
        data = fetch_json(url)

        if data and data.get('stat') == 'OK' and data.get('data'):
            rows = data['data']
            # 找台灣所有股票合計（最後一行通常是合計）
            total_row = None
            for row in rows:
                if '合計' in str(row[0]) or row[0].strip() == '':
                    total_row = row
                    break
            if not total_row:
                total_row = rows[-1]  # 取最後一行

            def parse_num(s):
                try:
                    return int(str(s).replace(',', '').replace(' ', ''))
                except:
                    return 0

            # 欄位：外資買超, 投信買超, 自營商買超（單位：千元）
            try:
                foreign_net = parse_num(total_row[4])   # 外資買賣超
                invest_net  = parse_num(total_row[7])   # 投信買賣超
                dealer_net  = parse_num(total_row[10])  # 自營商買賣超
                total_net   = parse_num(total_row[12]) if len(total_row) > 12 else 0

                result['date_institutional'] = date.strftime('%Y/%m/%d')
                result['foreign_net_buy'] = foreign_net         # 千元
                result['invest_net_buy']  = invest_net
                result['dealer_net_buy']  = dealer_net
                result['total_3_net_buy'] = total_net

                # 外資買超評分：-500億以下=0, +500億以上=10（單位千元，500億=50,000,000千元）
                # 實際上台股日均外資買賣在 -200億~+200億之間
                result['score_foreign'] = score(foreign_net, -20_000_000, 20_000_000)
                result['score_invest']  = score(invest_net,  -5_000_000,  5_000_000)

                print(f'   三大法人資料日期：{date.strftime("%Y/%m/%d")}')
                print(f'   外資：{foreign_net:,} 千元，投信：{invest_net:,} 千元')
                break
            except (IndexError, ValueError) as e:
                print(f'   [警告] 解析三大法人資料失敗：{e}')
                continue
        time.sleep(0.5)

    if not result:
        print('   [警告] 無法取得三大法人資料，使用預設值')
    return result

# ── TWSE 融資融券 ────────────────────────────────────────────
def get_twse_margin():
    """
    抓取融資融券餘額資料
    來源：TWSE opendata
    """
    print('→ 抓取融資融券資料...')
    today = datetime.date.today()
    result = {}

    for days_back in range(5):
        date = today - datetime.timedelta(days=days_back)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime('%Y%m%d')

        url = (
            f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN'
            f'?response=json&date={date_str}&selectType=CS'
        )
        data = fetch_json(url)

        if data and data.get('stat') == 'OK':
            tables = data.get('tables', [])
            if tables and len(tables) > 0:
                table = tables[0]
                rows = table.get('data', [])
                if rows:
                    # 最後一行為合計
                    last = rows[-1]
                    def pn(s):
                        try: return int(str(s).replace(',',''))
                        except: return 0

                    margin_balance = pn(last[1]) if len(last) > 1 else 0   # 融資餘額
                    short_balance  = pn(last[4]) if len(last) > 4 else 0   # 融券餘額

                    result['date_margin'] = date.strftime('%Y/%m/%d')
                    result['margin_balance'] = margin_balance  # 千股
                    result['short_balance']  = short_balance

                    print(f'   融資餘額：{margin_balance:,} 千股')
                    break
        time.sleep(0.5)

    if not result:
        print('   [警告] 無法取得融資融券資料')
    return result

# ── TAIFEX 外資期貨淨多單 ─────────────────────────────────────
def get_taifex_futures():
    """
    抓取台指期外資淨多單
    來源：TAIFEX 期交所
    """
    print('→ 抓取期交所外資期貨資料...')
    today = datetime.date.today()
    result = {}

    for days_back in range(5):
        date = today - datetime.timedelta(days=days_back)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime('%Y/%m/%d')

        url = (
            'https://www.taifex.com.tw/cht/3/futContractsDate'
        )
        # TAIFEX 用 POST 表單取資料
        params = urllib.parse.urlencode({
            'queryStartDate': date_str,
            'queryEndDate':   date_str,
            'commodityId':    'TXF',
        }).encode('utf-8')

        req = urllib.request.Request(url, data=params)
        req.add_header('User-Agent', 'Mozilla/5.0 (compatible; ETF-Monitor/1.0)')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='replace')

            # 簡單解析 HTML 表格，找外資的多空淨口數
            import re
            # 找含「外資」的行
            lines = html.split('\n')
            foreign_line = ''
            for i, line in enumerate(lines):
                if '外資及陸資' in line or '外資' in line:
                    # 取周圍幾行組合
                    foreign_line = ' '.join(lines[max(0,i):i+5])
                    break

            # 抓取數字
            nums = re.findall(r'[-]?\d{1,3}(?:,\d{3})*', foreign_line)
            clean = [int(n.replace(',','')) for n in nums if n]

            if len(clean) >= 6:
                # 通常格式：多方口數, 多方契約金額, 空方口數, 空方契約金額, 淨多單口數, ...
                net_long = clean[4] if len(clean) > 4 else 0
                result['date_futures'] = date.strftime('%Y/%m/%d')
                result['futures_foreign_net'] = net_long

                # 評分：-50000~+50000 口
                result['score_futures'] = score(net_long, -50000, 50000)
                print(f'   外資期貨淨多單：{net_long:,} 口')
                break
        except Exception as e:
            print(f'   [警告] TAIFEX 抓取失敗：{e}')
        time.sleep(0.5)

    if not result:
        print('   [警告] 無法取得期交所資料，使用中性預設值')
        result['score_futures'] = 5
    return result

# ── 主程式 ───────────────────────────────────────────────────
def main():
    print('=' * 50)
    print('台股 ETF 監控系統 - 資料抓取')
    print(f'執行時間：{datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")}')
    print('=' * 50)

    output = {
        'updated_at': datetime.datetime.now().strftime('%Y/%m/%d %H:%M'),
        'updated_ts': int(time.time()),
        'source': {
            'yahoo': 'Yahoo Finance',
            'twse': '台灣證交所',
            'taifex': '台灣期交所',
        }
    }

    # 抓取各來源資料
    yahoo = get_yahoo_data()
    output.update(yahoo)

    institutional = get_twse_institutional()
    output.update(institutional)

    margin = get_twse_margin()
    output.update(margin)

    futures = get_taifex_futures()
    output.update(futures)

    # ── 整合評分建議 ─────────────────────────────────────────
    # 彙整所有可自動取得的指標分數，供前端參考
    auto_scores = {}

    if 'score_usd_twd'  in output: auto_scores['s13'] = output['score_usd_twd']   # 台幣匯率
    if 'score_vix'      in output: auto_scores['s15'] = output['score_vix']        # VIX
    if 'score_sox'      in output: auto_scores['s16'] = output['score_sox']        # 費半
    if 'score_foreign'  in output: auto_scores['s2']  = output['score_foreign']    # 外資買超
    if 'score_invest'   in output: auto_scores['s3']  = output['score_invest']     # 投信買超
    if 'score_futures'  in output: auto_scores['s5']  = output['score_futures']    # 外資期貨

    output['auto_scores'] = auto_scores
    output['auto_score_count'] = len(auto_scores)

    # 寫出 data.json
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print('=' * 50)
    print(f'完成！已寫出 data.json')
    print(f'自動更新指標數：{len(auto_scores)} / 22')
    print('自動更新的指標：')
    label_map = {
        's2': '外資買超強度', 's3': '投信買超強度',
        's5': '外資期貨淨多單', 's13': '台幣匯率趨勢',
        's15': 'VIX 恐慌指數', 's16': '費半/那指走勢',
    }
    for k, v in auto_scores.items():
        print(f'  {label_map.get(k, k)}: {v}/10')
    print('=' * 50)

if __name__ == '__main__':
    main()
