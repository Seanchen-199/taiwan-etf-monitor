"""
台股多因子模型 - 建模、回測、優化引擎
輸入：raw_data.json
輸出：model_output.json（供 dashboard.html 讀取）
"""
import json, datetime, sys, subprocess, os

def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

for pkg in ['pandas', 'numpy', 'scikit-learn', 'scipy']:
    install(pkg)

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge, RidgeCV
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score
from sklearn.inspection import permutation_importance
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════
# 1. 資料載入與整合
# ══════════════════════════════════════════════════
def load_data():
    with open('raw_data.json', 'r', encoding='utf-8') as f:
        raw = json.load(f)

    series = raw.get('series', {})
    if not series:
        raise ValueError('raw_data.json 中無序列資料')

    df = pd.DataFrame(series)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    print(f'載入資料：{len(df)} 個交易日，{len(df.columns)} 個欄位')
    print(f'  時間範圍：{df.index[0].date()} ~ {df.index[-1].date()}')
    return df, raw

# ══════════════════════════════════════════════════
# 2. 特徵工程
# ══════════════════════════════════════════════════

# 因子權重分類（核心 vs 次要）
PRIMARY_FACTORS = [
    'SOX', 'US10Y', 'TWII', 'DXY', 'USDTWD',
    'margin_bal', 'foreign_net', 'invest_net',
    'top5_net', 'top10_net',
    'option_call_net', 'option_put_net',
    'futures_foreign_net',
]
SECONDARY_FACTORS = ['VIX', 'NASDAQ', 'DXY']

def build_features(df, lag_days=[1, 3, 5]):
    """
    特徵工程：
    1. 計算技術指標
    2. 正規化變動率（pct_change）
    3. 滯後特徵（lag features）
    4. 目標變數（TWII 5日後漲跌）
    """
    feat = pd.DataFrame(index=df.index)

    # ── 核心因子：轉為變動率 ──────────────────────
    price_cols = ['SOX', 'US10Y', 'TWII', 'DXY', 'USDTWD', 'NASDAQ', 'VIX',
                  'ETF0050', 'ETF006208']
    flow_cols  = ['foreign_net', 'invest_net', 'dealer_net', 'margin_bal',
                  'futures_foreign_net', 'option_call_net', 'option_put_net',
                  'top5_net', 'top10_net']

    for col in price_cols:
        if col in df.columns:
            feat[f'{col}_ret1']  = df[col].pct_change(1)
            feat[f'{col}_ret5']  = df[col].pct_change(5)
            feat[f'{col}_ret20'] = df[col].pct_change(20)

    for col in flow_cols:
        if col in df.columns:
            s = df[col].fillna(0)
            feat[f'{col}_raw']      = s
            feat[f'{col}_ma5']      = s.rolling(5).mean()
            feat[f'{col}_chg']      = s.diff(1)
            feat[f'{col}_zscore']   = (s - s.rolling(20).mean()) / (s.rolling(20).std() + 1e-9)

    # ── 次要因子：技術指標 ────────────────────────
    if 'TWII' in df.columns:
        twii = df['TWII']
        # MA
        feat['MA5']  = twii.rolling(5).mean()
        feat['MA20'] = twii.rolling(20).mean()
        feat['MA60'] = twii.rolling(60, min_periods=30).mean()
        feat['MA_cross_5_20']  = (twii / feat['MA5']  - 1)
        feat['MA_cross_5_60']  = (twii / feat['MA20'] - 1)
        # RSI(14)
        delta = twii.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        feat['RSI14'] = 100 - 100 / (1 + gain / (loss + 1e-9))
        # MACD
        ema12 = twii.ewm(span=12).mean()
        ema26 = twii.ewm(span=26).mean()
        macd  = ema12 - ema26
        feat['MACD']        = macd
        feat['MACD_signal'] = macd.ewm(span=9).mean()
        feat['MACD_hist']   = macd - feat['MACD_signal']
        # Bollinger
        ma20 = twii.rolling(20).mean()
        std20 = twii.rolling(20).std()
        feat['BB_upper'] = (twii - (ma20 + 2*std20)) / (2*std20 + 1e-9)
        feat['BB_lower'] = (twii - (ma20 - 2*std20)) / (2*std20 + 1e-9)

    # ── 滯後特徵（lag features）─────────────────
    base_cols = [c for c in feat.columns if not c.startswith('MA') and 'BB' not in c]
    for col in base_cols[:20]:  # 避免維度爆炸，取前20個
        for lag in lag_days:
            feat[f'{col}_lag{lag}'] = feat[col].shift(lag)

    # ── 目標變數：5日後 TWII 漲跌方向 ────────────
    if 'TWII' in df.columns:
        future_ret = df['TWII'].pct_change(5).shift(-5)
        feat['target_ret5']  = future_ret
        feat['target_dir5']  = (future_ret > 0).astype(int)  # 1=漲, 0=跌

    feat = feat.replace([np.inf, -np.inf], np.nan)
    feat = feat.dropna(thresh=int(len(feat.columns) * 0.5))

    print(f'特徵工程完成：{len(feat)} 筆，{len(feat.columns)} 個特徵')
    return feat

# ══════════════════════════════════════════════════
# 3. 共線性檢測
# ══════════════════════════════════════════════════
def check_multicollinearity(feat_df, threshold=0.85):
    """移除高度相關的特徵（Pearson |r| > threshold）"""
    feature_cols = [c for c in feat_df.columns
                    if c not in ('target_ret5', 'target_dir5')]
    X = feat_df[feature_cols].fillna(0)
    corr = X.corr().abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    to_drop = [col for col in upper.columns if any(upper[col] > threshold)]
    kept = [c for c in feature_cols if c not in to_drop]
    print(f'共線性檢測：移除 {len(to_drop)} 個高相關特徵，保留 {len(kept)} 個')
    return kept

# ══════════════════════════════════════════════════
# 4. 滾動式回測（Rolling Window Backtest）
# ══════════════════════════════════════════════════
def rolling_backtest(feat_df, feature_cols, train_size=40, step=5):
    """
    滾動式回測：
    - train_size: 訓練窗口（交易日）
    - step: 每次向前滾動的天數
    """
    print(f'滾動式回測：訓練窗口={train_size}日，步進={step}日...')
    X_all = feat_df[feature_cols].fillna(0).values
    y_dir = feat_df['target_dir5'].values
    y_ret = feat_df['target_ret5'].values
    dates = feat_df.index

    predictions = []
    actuals     = []
    pred_dates  = []
    pred_probs  = []

    n = len(feat_df)
    i = train_size
    while i + step <= n - 5:
        X_train = X_all[:i]
        y_train = y_dir[:i]

        # 標準化
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)

        # 模型：Ridge（快速，可解釋）
        model = GradientBoostingClassifier(
            n_estimators=50, max_depth=3,
            learning_rate=0.1, random_state=42
        )
        if len(np.unique(y_train)) < 2:
            i += step
            continue
        model.fit(X_train_s, y_train)

        # 預測下一個 step 的樣本
        for j in range(i, min(i + step, n - 5)):
            X_test_s = scaler.transform(X_all[j:j+1])
            pred = model.predict(X_test_s)[0]
            prob = model.predict_proba(X_test_s)[0][1]
            predictions.append(pred)
            actuals.append(y_dir[j])
            pred_probs.append(prob)
            pred_dates.append(dates[j])
        i += step

    if not predictions:
        return {}, None, None

    predictions = np.array(predictions)
    actuals     = np.array(actuals)
    pred_probs  = np.array(pred_probs)

    # ── 績效指標 ───────────────────────────────
    win_rate = accuracy_score(actuals, predictions)

    # 模擬策略報酬（信號正確+1, 錯誤-1，用 TWII 5日報酬）
    actual_rets = feat_df.loc[pred_dates, 'target_ret5'].values
    strategy_rets = np.where(predictions == 1, actual_rets, -actual_rets)
    cum_ret = np.cumprod(1 + strategy_rets)
    total_days = len(strategy_rets) * 5
    years = total_days / 252

    cagr = (cum_ret[-1] ** (1 / years) - 1) if years > 0 else 0

    # Max Drawdown
    peak = np.maximum.accumulate(cum_ret)
    dd   = (cum_ret - peak) / peak
    max_dd = dd.min()

    # Sharpe（假設無風險利率 1.5%）
    rf_daily = 0.015 / 252
    excess = strategy_rets - rf_daily
    sharpe = np.sqrt(252) * excess.mean() / (excess.std() + 1e-9)

    backtest_result = {
        'cagr':       round(float(cagr) * 100, 2),
        'max_dd':     round(float(max_dd) * 100, 2),
        'sharpe':     round(float(sharpe), 3),
        'win_rate':   round(float(win_rate) * 100, 2),
        'n_trades':   len(predictions),
        'total_return': round(float(cum_ret[-1] - 1) * 100, 2),
    }
    print(f'  回測結果：CAGR={cagr*100:.1f}% | MaxDD={max_dd*100:.1f}% | Sharpe={sharpe:.2f} | 勝率={win_rate*100:.1f}%')

    backtest_series = {
        'dates':    [str(d.date()) for d in pred_dates],
        'cum_ret':  [round(float(v), 4) for v in cum_ret],
        'probs':    [round(float(v), 3) for v in pred_probs],
    }
    return backtest_result, backtest_series, (predictions, actuals, pred_probs)

# ══════════════════════════════════════════════════
# 5. 最終模型 + 因子重要性
# ══════════════════════════════════════════════════
def train_final_model(feat_df, feature_cols):
    """用全部資料訓練最終模型，輸出因子重要性"""
    print('訓練最終模型...')
    X = feat_df[feature_cols].fillna(0)
    y = feat_df['target_dir5']

    valid_idx = y.notna()
    X, y = X[valid_idx], y[valid_idx]
    if len(X) < 20:
        return None, {}

    scaler = StandardScaler()
    X_s = scaler.fit_transform(X)

    # 時間序列交叉驗證
    tscv = TimeSeriesSplit(n_splits=3)
    cv_scores = []
    for tr, te in tscv.split(X_s):
        if len(np.unique(y.iloc[tr])) < 2: continue
        m = GradientBoostingClassifier(n_estimators=100, max_depth=3,
                                        learning_rate=0.05, random_state=42)
        m.fit(X_s[tr], y.iloc[tr])
        cv_scores.append(accuracy_score(y.iloc[te], m.predict(X_s[te])))

    print(f'  CV 準確率：{np.mean(cv_scores):.3f} ± {np.std(cv_scores):.3f}')

    # 最終模型
    model = GradientBoostingClassifier(n_estimators=100, max_depth=3,
                                        learning_rate=0.05, random_state=42)
    model.fit(X_s, y)

    # 因子重要性
    importances = model.feature_importances_
    feat_imp = sorted(zip(feature_cols, importances), key=lambda x: -x[1])

    # 當前預測
    latest_X = X.iloc[[-1]]
    latest_X_s = scaler.transform(latest_X)
    pred_class = int(model.predict(latest_X_s)[0])
    pred_prob  = float(model.predict_proba(latest_X_s)[0][1])

    return model, {
        'feature_importance': [{'factor': k, 'importance': round(float(v)*100, 2)}
                                for k, v in feat_imp[:20]],
        'cv_accuracy':   round(float(np.mean(cv_scores)), 3) if cv_scores else None,
        'prediction':    pred_class,
        'probability':   round(pred_prob, 3),
        'signal':        '多方' if pred_prob >= 0.6 else '空方' if pred_prob <= 0.4 else '中性',
        'confidence':    '高' if abs(pred_prob - 0.5) > 0.15 else '中' if abs(pred_prob - 0.5) > 0.08 else '低',
    }

# ══════════════════════════════════════════════════
# 6. 市場狀態評估（基於最新資料）
# ══════════════════════════════════════════════════
def assess_market_state(raw):
    """基於最新資料的定性市場狀態評估"""
    latest  = raw.get('latest', {})
    twse_l  = raw.get('twse_latest', {})
    taifex_l = raw.get('taifex_latest', {})

    scores = {}

    # SOX 費半
    if 'SOX' in latest:
        chg = latest['SOX'].get('change_pct', 0) or 0
        scores['SOX'] = {'score': min(10, max(0, 5 + chg)), 'value': latest['SOX']['price'], 'change': chg}

    # 美10年債（殖利率上升=壓力，下降=正面）
    if 'US10Y' in latest:
        chg = latest['US10Y'].get('change_pct', 0) or 0
        scores['US10Y'] = {'score': min(10, max(0, 5 - chg * 2)), 'value': latest['US10Y']['price'], 'change': chg}

    # DXY（美元強=新興市場壓力）
    if 'DXY' in latest:
        chg = latest['DXY'].get('change_pct', 0) or 0
        scores['DXY'] = {'score': min(10, max(0, 5 - chg * 2)), 'value': latest['DXY']['price'], 'change': chg}

    # 台幣（升值=正面）
    if 'USDTWD' in latest:
        price = latest['USDTWD']['price']
        chg   = latest['USDTWD'].get('change_pct', 0) or 0
        scores['USDTWD'] = {
            'score':  round(min(10, max(0, (34 - price) / 4 * 10)), 1),
            'value':  price, 'change': chg,
            'trend':  '升值' if chg < 0 else '貶值'
        }

    # VIX
    if 'VIX' in latest:
        vix = latest['VIX']['price']
        if vix > 40:   vs, sc = '極度恐慌（底部機會）', 8
        elif vix > 30: vs, sc = '恐慌', 3
        elif vix > 25: vs, sc = '警戒', 4
        elif vix > 20: vs, sc = '偏高', 5
        elif vix > 15: vs, sc = '平穩', 7
        else:          vs, sc = '極度平靜', 8
        scores['VIX'] = {'score': sc, 'value': vix, 'status': vs}

    # 外資買賣超
    fn = twse_l.get('foreign_net')
    if fn is not None:
        scores['ForeignNet'] = {
            'score': round(min(10, max(0, 5 + fn / 4_000_000)), 1),
            'value': fn, 'unit': '千元'
        }

    # 融資餘額（融資遞增=多，但過高=危險）
    mb = twse_l.get('margin_bal')
    if mb is not None:
        scores['MarginBal'] = {'score': 5, 'value': mb, 'unit': '千股'}  # 需要對比歷史才能評分

    # 外資期貨淨多單
    fn_fut = taifex_l.get('futures_foreign_net')
    if fn_fut is not None:
        scores['FuturesForeignNet'] = {
            'score': round(min(10, max(0, 5 + fn_fut / 10000)), 1),
            'value': fn_fut, 'unit': '口'
        }

    # 前五大交易人（多單>空單=多方）
    t5 = taifex_l.get('top5_net')
    if t5 is not None:
        scores['Top5Net'] = {
            'score': round(min(10, max(0, 5 + t5 / 5000)), 1),
            'value': t5, 'unit': '口'
        }

    # 前十大交易人
    t10 = taifex_l.get('top10_net')
    if t10 is not None:
        scores['Top10Net'] = {
            'score': round(min(10, max(0, 5 + t10 / 8000)), 1),
            'value': t10, 'unit': '口'
        }

    # P/C Ratio
    pc = taifex_l.get('pc_ratio')
    if pc is not None:
        # P/C > 1.2 = 悲觀過頭 = 反向正面
        if pc > 1.5:   pc_score, pc_status = 8, '悲觀過頭（反向正面）'
        elif pc > 1.2: pc_score, pc_status = 7, '偏悲觀'
        elif pc > 0.8: pc_score, pc_status = 5, '正常'
        elif pc > 0.5: pc_score, pc_status = 4, '偏樂觀'
        else:           pc_score, pc_status = 2, '過度樂觀（注意）'
        scores['PCRatio'] = {'score': pc_score, 'value': round(pc, 3), 'status': pc_status}

    # 加權平均
    weights = {
        'SOX': 0.12, 'US10Y': 0.10, 'DXY': 0.08, 'USDTWD': 0.08, 'VIX': 0.06,
        'ForeignNet': 0.12, 'MarginBal': 0.06, 'FuturesForeignNet': 0.10,
        'Top5Net': 0.10, 'Top10Net': 0.08, 'PCRatio': 0.10
    }
    total_w = sum(weights[k] for k in scores if k in weights)
    composite = sum(scores[k]['score'] * weights.get(k, 0.05) for k in scores if k in weights)
    composite = composite / total_w * 10 if total_w > 0 else 50
    composite = round(composite, 1)

    if composite >= 7.0:   verdict, action = '積極做多', '建議積極加碼 0050 / 006208（+20~30%）'
    elif composite >= 6.0: verdict, action = '多方偏強', '建議小幅加碼（+10%），持續觀察'
    elif composite >= 4.5: verdict, action = '中性觀望', '維持標準倉位，暫緩加碼'
    elif composite >= 3.5: verdict, action = '審慎減碼', '建議減碼 10~20%，控制風險'
    else:                  verdict, action = '空方偏強', '建議大幅減碼或暫時空手'

    return {
        'composite_score': composite,
        'verdict':         verdict,
        'action':          action,
        'factor_scores':   scores,
    }

# ══════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════
def main():
    print('='*55)
    print('台股多因子模型 - 建模引擎')
    print(f'時間：{datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")}')
    print('='*55)

    df, raw = load_data()
    feat_df  = build_features(df)
    feature_cols = check_multicollinearity(feat_df)
    feature_cols = [c for c in feature_cols
                    if c not in ('target_ret5', 'target_dir5')]

    backtest_result, backtest_series, _ = rolling_backtest(feat_df, feature_cols)
    _, model_result = train_final_model(feat_df, feature_cols)
    market_state = assess_market_state(raw)

    output = {
        'updated_at':     raw.get('updated_at'),
        'updated_ts':     raw.get('updated_ts'),
        'market_state':   market_state,
        'model':          model_result,
        'backtest':       backtest_result,
        'backtest_series': backtest_series,
        'latest':         raw.get('latest', {}),
        'twse_latest':    raw.get('twse_latest', {}),
        'taifex_latest':  raw.get('taifex_latest', {}),
    }

    with open('model_output.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print()
    print('='*55)
    print(f'模型輸出完成 → model_output.json')
    ms = market_state
    print(f'市場狀態：{ms["verdict"]}（{ms["composite_score"]}/10）')
    print(f'模型訊號：{model_result.get("signal","--")}（機率：{model_result.get("probability","--")}，信心：{model_result.get("confidence","--")}）')
    if backtest_result:
        print(f'回測績效：CAGR={backtest_result["cagr"]}% | Sharpe={backtest_result["sharpe"]} | 勝率={backtest_result["win_rate"]}%')
    print('='*55)

    # Discord 推播
    webhook = os.environ.get('DISCORD_WEBHOOK')
    if webhook and model_result:
        import requests as req
        sig   = model_result.get('signal', '--')
        prob  = model_result.get('probability', 0)
        conf  = model_result.get('confidence', '--')
        score = ms['composite_score']
        verdict = ms['verdict']
        color = 0x22c97a if sig == '多方' else 0xf05252 if sig == '空方' else 0x6b8cba

        fields = [
            {'name': '模型訊號',     'value': f'**{sig}**（機率 {prob:.1%}，信心 {conf}）', 'inline': False},
            {'name': '市場綜合評分', 'value': f'{score}/10 — {verdict}', 'inline': True},
            {'name': '操作建議',     'value': ms['action'], 'inline': False},
        ]
        if backtest_result:
            fields.append({
                'name': '回測績效摘要',
                'value': f'CAGR {backtest_result["cagr"]}% ｜ 最大回撤 {backtest_result["max_dd"]}% ｜ Sharpe {backtest_result["sharpe"]} ｜ 勝率 {backtest_result["win_rate"]}%',
                'inline': False
            })

        payload = {'embeds': [{'title': '📊 台股多因子模型 · 每日訊號', 'color': color,
                               'fields': fields,
                               'footer': {'text': f'更新：{raw.get("updated_at","--")}'}}]}
        try:
            r = req.post(webhook, json=payload, timeout=10)
            print(f'Discord 推播：{"成功" if r.status_code in (200,204) else "失敗 "+str(r.status_code)}')
        except Exception as e:
            print(f'Discord 推播錯誤：{e}')

if __name__ == '__main__':
    main()
