#!/usr/bin/env python3
"""스윙 탐색용 시세 수집기 (market-inbox 공개 저장소에서 GitHub Actions로 실행).

하는 일: 한국(코스피 시총 상위 200 + 코스닥 시총 상위 150) 또는
미국(S&P 500 + 나스닥 100) 종목의 일별 시세를 받아 업종 강도와 종목 지표를
계산하고 swing/<시장>.md 요약 표와 swing/<시장>_all.csv 를 쓴다.

공개 저장소용이다. 시장 자료만 다루고 개인 정보·보유 종목은 넣지 않는다.
열쇠(API 키)나 로그인은 쓰지 않는다.
"""
import argparse
import datetime as dt
import io
import math
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
MIN_ROWS = 61                      # 지표 계산에 필요한 최소 거래일 수
KR_BIO = ("제약", "생물공학", "생명과학")
US_BIO = ("Biotechnology", "Pharmaceuticals", "Life Sciences Tools & Services")
KR_NAME_SKIP = re.compile(r"(스팩|리츠|\d호$|우$|우B$|우C$|\(전환\)|^KODEX|^TIGER|^ACE |^RISE |"
                          r"^SOL |^HANARO|^KOSEF|^PLUS |^ARIRANG|^KBSTAR|^TIMEFOLIO|^KIWOOM|ETN)")
PROBLEMS = []                      # 막힌 곳 기록 (요약 파일 끝에 적는다)


def note(msg):
    PROBLEMS.append(msg)
    print("[막힘]", msg, file=sys.stderr)


def get(url, enc=None, tries=3, timeout=15):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content.decode(enc, errors="ignore") if enc else r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:      # 네트워크 오류는 다시 시도
            last = type(e).__name__
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]} ({last})")


# ───────────────────────── 한국 (네이버 금융) ─────────────────────────
def kr_sector_map():
    """종목코드 → 네이버 업종명."""
    base = "https://finance.naver.com/sise/"
    html = get(base + "sise_group.naver?type=upjong", enc="euc-kr")
    groups = re.findall(r'sise_group_detail\.naver\?type=upjong&(?:amp;)?no=(\d+)"[^>]*>([^<]+)</a>', html)
    out = {}
    for no, name in groups:
        try:
            page = get(base + f"sise_group_detail.naver?type=upjong&no={no}", enc="euc-kr")
        except RuntimeError as e:
            note(f"업종 페이지 실패: {name.strip()} {e}")
            continue
        for code in set(re.findall(r"/item/main\.naver\?code=(\d{6})", page)):
            out.setdefault(code, name.strip())
        time.sleep(0.15)
    return out


def kr_ranked(sosok, pages):
    """시가총액 순위 페이지에서 (코드, 이름) 목록. sosok 0=코스피, 1=코스닥."""
    rows, seen = [], set()
    for p in range(1, pages + 1):
        html = get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={p}", enc="euc-kr")
        for code, name in re.findall(r'/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>', html):
            if code not in seen:
                seen.add(code)
                rows.append((code, name.strip()))
        time.sleep(0.15)
    return rows


def kr_universe():
    sectors = kr_sector_map()
    if len(sectors) < 500:
        raise RuntimeError(f"업종 표가 너무 적음({len(sectors)})")
    uni = []
    for sosok, market, want in ((0, "KOSPI", 200), (1, "KOSDAQ", 150)):
        n = 0
        for code, name in kr_ranked(sosok, pages=math.ceil(want / 50) + 3):
            if n >= want:
                break
            if KR_NAME_SKIP.search(name) or not code.endswith("0") or code not in sectors:
                continue                      # 우선주·ETF·스팩 등 제외
            n += 1                            # 바이오는 순위에는 세고 탐색에서만 뺀다
            if any(k in sectors[code] for k in KR_BIO):
                continue
            uni.append({"ticker": code, "name": name, "market": market, "sector": sectors[code]})
    return pd.DataFrame(uni)


def kr_prices(symbol, days=420):
    end = dt.datetime.now(KST).date()
    start = end - dt.timedelta(days=days)
    url = ("https://api.finance.naver.com/siseJson.naver?"
           f"symbol={symbol}&requestType=1&startTime={start:%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day")
    txt = get(url)
    rows = re.findall(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', txt)
    if not rows:
        raise RuntimeError(f"시세 없음 {symbol}")
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").astype(float).sort_index()


def collect_kr(cache_path):
    try:
        uni = kr_universe()
        uni.to_csv(cache_path, index=False)
    except Exception as e:
        note(f"종목 목록 새로 받기 실패({e}) → 저장해 둔 목록 사용")
        uni = pd.read_csv(cache_path, dtype={"ticker": str})
    index = {}
    for sym in ("KOSPI", "KOSDAQ"):
        try:
            index[sym] = kr_prices(sym)
        except Exception as e:
            note(f"지수 시세 실패: {sym} {e}")
    prices = {}
    for t in uni["ticker"]:
        try:
            prices[t] = kr_prices(t)
        except Exception as e:
            note(f"종목 시세 실패: {t} {e}")
        time.sleep(0.05)
    return uni, prices, index


# ───────────────────────── 미국 (위키백과 목록 + 야후) ─────────────────────────
def _wiki_table(url, need):
    for t in pd.read_html(io.StringIO(get(url))):
        cols = [str(c) for c in t.columns]
        if any(n in cols for n in need):
            t.columns = cols
            return t
    raise RuntimeError("표를 찾지 못함 " + url)


def us_universe():
    sp = _wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("Symbol",))
    uni = pd.DataFrame({"ticker": sp["Symbol"], "name": sp["Security"],
                        "market": "S&P500", "sector": sp["GICS Sub-Industry"]})
    try:
        nd = _wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100", ("Ticker", "Symbol"))
        tcol = "Ticker" if "Ticker" in nd.columns else "Symbol"
        ncol = next((c for c in nd.columns if c.lower() in ("company", "security")), tcol)
        scol = next((c for c in nd.columns if "sub" in c.lower()), None) or \
            next((c for c in nd.columns if "sector" in c.lower() or "industry" in c.lower()), None)
        extra = nd[~nd[tcol].isin(uni["ticker"])]
        uni = pd.concat([uni, pd.DataFrame({
            "ticker": extra[tcol], "name": extra[ncol], "market": "NASDAQ100",
            "sector": extra[scol] if scol else "기타"})], ignore_index=True)
    except Exception as e:
        note(f"나스닥 100 목록 실패({e}) → S&P 500만 사용")
    uni["ticker"] = uni["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)
    uni = uni[~uni["sector"].isin(US_BIO)].drop_duplicates("ticker").reset_index(drop=True)
    if len(uni) < 300:
        raise RuntimeError(f"종목 목록이 너무 적음({len(uni)})")
    return uni


def us_prices(tickers):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            raw = yf.download(chunk, period="14mo", interval="1d", auto_adjust=True,
                              group_by="ticker", threads=True, progress=False)
        except Exception as e:
            note(f"야후 묶음 실패({i}~): {type(e).__name__}")
            continue
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
                if len(df):
                    out[t] = df
            except Exception:
                pass
        time.sleep(1)
    return out


def collect_us(cache_path):
    try:
        uni = us_universe()
        uni.to_csv(cache_path, index=False)
    except Exception as e:
        note(f"종목 목록 새로 받기 실패({e}) → 저장해 둔 목록 사용")
        uni = pd.read_csv(cache_path, dtype={"ticker": str})
    prices = us_prices(list(uni["ticker"]) + ["SPY", "QQQ"])
    index = {k: prices.pop(k) for k in ("SPY", "QQQ") if k in prices}
    for t in uni["ticker"]:
        if t not in prices:
            note(f"종목 시세 실패: {t}")
    return uni, prices, index


# ───────────────────────── 계산 ─────────────────────────
def metrics(df):
    """한 종목(또는 지수)의 지표. 자료가 모자라면 None."""
    if df is None or len(df) < MIN_ROWS:
        return None
    c, h, l, v = df["close"].values, df["high"].values, df["low"].values, df["volume"].values
    ret = lambda n, end=-1: c[end] / c[end - n] - 1
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    vol20 = v[-21:-1].mean()
    return {
        "date": df.index[-1].date(), "close": c[-1],
        "r1": ret(1), "r5": ret(5), "r20": ret(20), "r60": ret(60),
        "r20_prev": ret(20, -6),                                  # 5거래일 전 시점의 20일 수익률
        "vol_x": v[-1] / vol20 if vol20 > 0 else np.nan,          # 거래량 배수(직전 20일 평균 대비)
        "value20": float((c[-20:] * v[-20:]).mean()),             # 20일 평균 거래대금
        "off_high": c[-1] / h[-252:].max() - 1,                   # 52주(자료 범위 내) 고점 대비
        "new20": bool(c[-1] >= c[-20:].max()),                    # 20일 종가 신고가
        "above20": bool(c[-1] > c[-20:].mean()),
        "atr": tr[-14:].mean() / c[-1],
    }


def build(uni, prices, index, market):
    idx = {k: metrics(v) for k, v in index.items()}
    bench = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "S&P500": "SPY", "NASDAQ100": "QQQ"}
    rows = []
    for u in uni.to_dict("records"):
        m = metrics(prices.get(u["ticker"]))
        if not m:
            continue
        b = idx.get(bench[u["market"]])
        m["rs20"] = m["r20"] - b["r20"] if b else np.nan          # 지수 대비 20일 초과수익
        m["rs20_prev"] = m["r20_prev"] - b["r20_prev"] if b else np.nan
        rows.append({**u, **m})
    df = pd.DataFrame(rows)
    if df.empty:
        return df, pd.DataFrame(), idx
    day = df["date"].mode().iloc[0]
    df = df[df["date"] == day].reset_index(drop=True)             # 기준 거래일이 다른(거래정지 등) 종목 제외
    g = df.groupby("sector")
    sec = pd.DataFrame({
        "n": g.size(), "rs20": g["rs20"].median(), "rs20_prev": g["rs20_prev"].median(),
        "r5": g["r5"].median(), "breadth": g["above20"].mean()})
    sec = sec[sec["n"] >= 3]
    sec["delta"] = sec["rs20"] - sec["rs20_prev"]                 # 5거래일 사이 상대강도 변화
    return df, sec.sort_values("delta", ascending=False), idx


# ───────────────────────── 요약 파일 ─────────────────────────
def pct(x, d=1):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def stock_rows(d, kr):
    out = ["| 종목 | 업종 | 종가 | 1일 | 5일 | 20일 | 지수대비20일 | 거래량배수 | 고점대비 | 하루변동폭 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in d.to_dict("records"):
        price = f"{r['close']:,.0f}" if kr else f"{r['close']:,.2f}"
        out.append(f"| {r['name']}({r['ticker']}) | {r['sector']} | {price} | {pct(r['r1'])} | {pct(r['r5'])} | "
                   f"{pct(r['r20'])} | {pct(r['rs20'])} | {r['vol_x']:.1f}배 | {pct(r['off_high'])} | {pct(r['atr']).lstrip('+')} |")
    return out


def sector_rows(s):
    out = ["| 업종 | 종목수 | 지수대비20일 | 5일전보다 | 5일 | 20일선 위 비율 |", "|---|---|---|---|---|---|"]
    for name, r in s.iterrows():
        out.append(f"| {name} | {int(r['n'])} | {pct(r['rs20'])} | {pct(r['delta'])}p | {pct(r['r5'])} | {r['breadth'] * 100:.0f}% |")
    return out


def render(market, uni, df, sec, idx, now):
    kr = market == "kr"
    total, ok = len(uni), len(df)
    day = df["date"].iloc[0] if ok else None
    ratio = ok / total if total else 0
    idx_ok = all(idx.get(k) for k in (("KOSPI", "KOSDAQ") if kr else ("SPY", "QQQ")))
    status = "실패" if ratio < 0.6 or not ok else ("정상" if ratio >= 0.9 and idx_ok else "축약")
    scope = ("한국: 코스피 시총 상위 200 + 코스닥 시총 상위 150 (우선주·ETF·스팩·바이오 제외)" if kr
             else "미국: S&P 500 + 나스닥 100 (바이오·제약 제외), 지수는 SPY·QQQ로 대신함")
    L = [f"작성 시각: {now:%Y-%m-%d %H:%M} KST",
         f"기준 거래일: {day if day else '없음'}",
         f"상태: {status}", "",
         f"# 스윙 탐색용 시세 요약 ({'한국' if kr else '미국'})",
         f"- 범위: {scope}",
         f"- 수집: {ok}종목 계산 / 대상 {total}종목",
         "- 값은 계산 결과이며 사실 자료다. 매수·매도 의견이 아니다."]
    for k, m in idx.items():
        if m:
            L.append(f"- {k}: {m['close']:,.2f} · 1일 {pct(m['r1'])} · 5일 {pct(m['r5'])} · 20일 {pct(m['r20'])} · 60일 {pct(m['r60'])}")
    if status == "실패":
        L += ["", "## 막힌 곳"] + [f"- {p}" for p in PROBLEMS[:15]]
        return "\n".join(L) + "\n", status
    liquid = df[df["value20"] >= (5e9 if kr else 2e7)]            # 거래대금 기준: 한국 50억원, 미국 2천만 달러
    rising = sec[sec["delta"] > 0].head(10)
    L += ["", "## 강해지는 업종 (5거래일 사이 지수 대비 강도가 가장 많이 오른 순)"] + sector_rows(rising)
    L += ["", "## 이미 강한 업종 (지수 대비 20일 강도 상위 5)"] + sector_rows(sec.sort_values("rs20", ascending=False).head(5))
    L += ["", "## 약한 업종 (지수 대비 20일 강도 하위 5)"] + sector_rows(sec.sort_values("rs20").head(5))
    picks = pd.concat([liquid[liquid["sector"] == s].sort_values("rs20", ascending=False).head(3)
                       for s in rising.index[:6]]) if len(rising) else liquid.head(0)
    L += ["", "## 강해지는 업종 상위 6개의 종목 (업종마다 지수 대비 강도 상위 3)"] + stock_rows(picks, kr)
    surge = liquid[(liquid["vol_x"] >= 2) & (liquid["r1"] > 0)].sort_values("vol_x", ascending=False).head(10)
    L += ["", "## 거래량 급증 (직전 20일 평균의 2배 이상이면서 상승 마감, 상위 10)"] + stock_rows(surge, kr)
    highs = liquid[liquid["new20"] & (liquid["off_high"] >= -0.05)].sort_values("rs20", ascending=False).head(8)
    L += ["", "## 20일 신고가이면서 52주 고점 5% 이내 (지수 대비 강도 상위 8)"] + stock_rows(highs, kr)
    L += ["", "## 열 설명",
          "- 지수대비20일: 종목 20일 수익률에서 지수 20일 수익률을 뺀 값. 업종은 소속 종목의 중앙값.",
          "- 5일전보다: 지수대비20일이 5거래일 전보다 얼마나 변했는지(%p).",
          "- 거래량배수: 기준일 거래량 ÷ 직전 20일 평균. 고점대비: 최근 1년 고가 대비.",
          "- 하루변동폭: 최근 14일 평균 일중 변동폭 ÷ 종가."]
    if PROBLEMS:
        L += ["", "## 막힌 곳"] + [f"- {p}" for p in PROBLEMS[:15]]
        if len(PROBLEMS) > 15:
            L.append(f"- 그 밖에 {len(PROBLEMS) - 15}건")
    return "\n".join(L) + "\n", status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("kr", "us"), required=True)
    ap.add_argument("--out", default="swing")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    now = dt.datetime.now(KST)
    cache = os.path.join(a.out, f"universe_{a.market}.csv")
    try:
        uni, prices, index = (collect_kr if a.market == "kr" else collect_us)(cache)
        df, sec, idx = build(uni, prices, index, a.market)
    except Exception as e:                                        # 통째로 실패해도 '실패' 파일은 남긴다
        note(f"수집 중단: {type(e).__name__}: {e}")
        uni, df, sec, idx = pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}
    text, status = render(a.market, uni, df, sec, idx, now)
    with open(os.path.join(a.out, f"{a.market}.md"), "w", encoding="utf-8") as f:
        f.write(text)
    if len(df):
        df.round(4).to_csv(os.path.join(a.out, f"{a.market}_all.csv"), index=False)
    print(f"{a.market}: 상태 {status}, 계산 {len(df)}종목, 막힌 곳 {len(PROBLEMS)}건")


if __name__ == "__main__":
    main()
