#!/usr/bin/env python3
"""스윙 탐색용 시세 수집기 v2 (market-inbox 공개 저장소에서 GitHub Actions로 실행).

하는 일: 한국(코스피 시총 상위 200 + 코스닥 시총 상위 150) 또는
미국(S&P 500 + 나스닥 100) 종목의 일별 시세를 받아 업종 강도와 종목 지표를
계산하고 swing/<시장>.md 요약 표와 swing/<시장>_all.csv 를 쓴다.

공개 저장소용이다. 시장 자료만 다루고 개인 정보·보유 종목은 넣지 않는다.
열쇠(API 키)나 로그인은 쓰지 않는다.
"""
import argparse
import datetime as dt
import io
import json
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
KR_BIO = ("제약", "생물공학", "생명과학", "의약", "생물학", "자연과학 및 공학 연구개발")
US_BIO = ("Biotechnology", "Pharmaceuticals", "Life Sciences Tools & Services")
KR_NAME_SKIP = re.compile(r"(스팩|리츠|\d호$|우$|우B$|우C$|\(전환\)|^KODEX|^TIGER|^ACE |^RISE |"
                          r"^SOL |^HANARO|^KOSEF|^PLUS |^ARIRANG|^KBSTAR|^TIMEFOLIO|^KIWOOM|ETN)")
PROBLEMS = []                      # 막힌 곳 기록 (요약 파일 끝에 적는다)
PROBE = []                         # 조달처별 시도 결과 (swing/probe_<시장>.md 에 적는다)
CODE_KEYS = ("itemCode", "stockCode", "code", "reutersCode", "symbolCode")
NAME_KEYS = ("stockName", "itemName", "name", "korName")
GROUP_KEYS = ("no", "industryCode", "groupCode", "upjongCode", "id")
NAVER = "https://m.stock.naver.com/api/stocks/"


def note(msg):
    PROBLEMS.append(msg)
    print("[막힘]", msg, file=sys.stderr)


def probe(what, result):
    PROBE.append(f"- {what}: {result}")
    print("[조달]", what, result, file=sys.stderr)


def jget(url):
    text = get(url)
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"JSON 아님: {text[:160]!r}")


def find_rows(obj, keys):
    """JSON 안에서 keys 중 하나를 가진 dict 들의 첫 목록을 찾는다(응답 모양이 바뀌어도 버티게)."""
    queue = [obj]
    while queue:
        o = queue.pop(0)
        if isinstance(o, list):
            if o and isinstance(o[0], dict) and any(k in o[0] for k in keys):
                return o
            queue.extend(x for x in o if isinstance(x, (dict, list)))
        elif isinstance(o, dict):
            queue.extend(v for v in o.values() if isinstance(v, (dict, list)))
    return []


def pick(d, keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return str(d[k]).strip()
    return ""


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


# ───────────────────────── 한국 (목록: 네이버 JSON·KIND / 시세: 야후) ─────────────────────────
def kr_ranked_naver(market, want):
    """네이버 시가총액 순위 JSON → [(코드, 이름)]."""
    rows = []
    for p in range(1, math.ceil(want / 100) + 3):
        items = find_rows(jget(f"{NAVER}marketValue/{market}?page={p}&pageSize=100"), CODE_KEYS)
        if p == 1:
            probe(f"네이버 시총 순위 {market}", f"{len(items)}행, 열 {sorted(items[0])[:14] if items else '없음'}")
        if not items:
            break
        for it in items:
            if str(it.get("stockEndType", "stock")).lower() != "stock":
                continue
            rows.append((pick(it, CODE_KEYS), pick(it, NAME_KEYS)))
        time.sleep(0.2)
    if len(rows) < want:
        raise RuntimeError(f"순위 {len(rows)}행뿐")
    return rows


def kr_ranked_fdr(market):
    """대체: FinanceDataReader 상장 목록(시가총액 순)."""
    try:
        import FinanceDataReader as fdr
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "finance-datareader"], check=True)
        import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    probe("FinanceDataReader 목록", f"{len(df)}행, 열 {list(df.columns)[:12]}")
    df = df[df["Market"].astype(str).str.upper().str.startswith(market)].sort_values("Marcap", ascending=False)
    return list(zip(df["Code"].astype(str), df["Name"].astype(str)))


def kr_sectors_naver():
    """네이버 업종 JSON → {코드: 업종명}."""
    groups, p = [], 1
    while p <= 3:
        got = find_rows(jget(f"{NAVER}industry?page={p}&pageSize=100"), GROUP_KEYS)
        if p == 1:
            probe("네이버 업종 목록", f"{len(got)}행, 열 {sorted(got[0])[:14] if got else '없음'}")
        if not got:
            break
        groups += got
        if len(got) < 100:
            break
        p += 1
    out = {}
    for i, g in enumerate(groups):
        gid, gname = pick(g, GROUP_KEYS), pick(g, ("name", "industryName", "groupName", "upjongName"))
        if not gid or not gname:
            continue
        for p in range(1, 6):
            try:
                items = find_rows(jget(f"{NAVER}industry/{gid}?page={p}&pageSize=100"), CODE_KEYS)
            except RuntimeError as e:
                if i == 0:
                    probe("네이버 업종 상세", f"실패 {e}")
                break
            if i == 0 and p == 1:
                probe("네이버 업종 상세", f"{gname}: {len(items)}행, 열 {sorted(items[0])[:10] if items else '없음'}")
            for it in items:
                out.setdefault(pick(it, CODE_KEYS), gname)
            if len(items) < 100:
                break
        time.sleep(0.1)
    if len(out) < 500:
        raise RuntimeError(f"업종 표 {len(out)}종목뿐")
    return out


def kr_sectors_kind():
    """대체: 한국거래소 KIND 상장법인 목록의 업종."""
    out = {}
    for mk in ("stockMkt", "kosdaqMkt"):
        html = get("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13&marketType=" + mk,
                   enc="euc-kr", timeout=40)
        t = pd.read_html(io.StringIO(html), converters={"종목코드": str})[0]
        for code, sec in zip(t["종목코드"], t["업종"]):
            out[str(code).strip().zfill(6)] = str(sec).strip()
    probe("KIND 상장법인 목록", f"{len(out)}종목")
    if len(out) < 500:
        raise RuntimeError(f"KIND 표 {len(out)}종목뿐")
    return out


def first_ok(label, *funcs):
    """여러 조달처를 차례로 시도해 처음 성공한 결과를 쓴다."""
    for name, fn in funcs:
        try:
            r = fn()
            probe(label, f"{name} 사용")
            return r
        except Exception as e:
            probe(label, f"{name} 실패 — {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError(f"{label}: 모든 조달처 실패")


def kr_universe():
    sectors = first_ok("한국 업종", ("네이버", kr_sectors_naver), ("KIND", kr_sectors_kind))
    uni = []
    for market, want in (("KOSPI", 200), ("KOSDAQ", 150)):
        ranked = first_ok(f"한국 순위 {market}",
                          ("네이버", lambda m=market, w=want: kr_ranked_naver(m, w + 80)),
                          ("FinanceDataReader", lambda m=market: kr_ranked_fdr(m)))
        n = 0
        for code, name in ranked:
            if n >= want:
                break
            if KR_NAME_SKIP.search(name) or not code.endswith("0") or code not in sectors:
                continue                      # 우선주·ETF·스팩 등 제외
            n += 1                            # 바이오는 순위에는 세고 탐색에서만 뺀다
            if any(k in sectors[code] for k in KR_BIO):
                continue
            uni.append({"ticker": code, "name": name, "market": market, "sector": sectors[code]})
    if len(uni) < 150:
        raise RuntimeError(f"종목 목록이 너무 적음({len(uni)})")
    return pd.DataFrame(uni)


def kr_prices_naver(symbol, days=420):
    """대체 시세: 네이버 차트(두 주소를 차례로)."""
    end = dt.datetime.now(KST).date()
    q = f"symbol={symbol}&requestType=1&startTime={end - dt.timedelta(days=days):%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day"
    for base in ("https://m.stock.naver.com/front-api/external/chart/domestic/info?",
                 "https://api.finance.naver.com/siseJson.naver?"):
        try:
            txt = get(base + q, tries=2)
        except RuntimeError:
            continue
        rows = re.findall(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', txt)
        if rows:
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date").astype(float).sort_index()
    raise RuntimeError(f"시세 없음 {symbol}")


def collect_kr(cache_path):
    try:
        uni = kr_universe()
        uni.to_csv(cache_path, index=False)
    except Exception as e:
        note(f"종목 목록 새로 받기 실패({e}) → 저장해 둔 목록 사용")
        uni = pd.read_csv(cache_path, dtype={"ticker": str})
    suffix = {"KOSPI": ".KS", "KOSDAQ": ".KQ"}
    ymap = {t + suffix[m]: t for t, m in zip(uni["ticker"], uni["market"])}
    raw = yahoo_prices(list(ymap) + ["^KS11", "^KQ11"])
    index = {k: raw.pop(y) for k, y in (("KOSPI", "^KS11"), ("KOSDAQ", "^KQ11")) if y in raw}
    prices = {ymap[y]: df for y, df in raw.items() if y in ymap}
    probe("한국 시세(야후)", f"{len(prices)}/{len(uni)}종목, 지수 {sorted(index)}")
    missing = [t for t in uni["ticker"] if t not in prices]
    naver_ok = 0
    for t in missing[:400]:
        try:
            prices[t] = kr_prices_naver(t)
            naver_ok += 1
        except Exception:
            note(f"종목 시세 실패: {t}")
        time.sleep(0.05)
    for k in ("KOSPI", "KOSDAQ"):
        if k not in index:
            try:
                index[k] = kr_prices_naver(k)
                naver_ok += 1
            except Exception as e:
                note(f"지수 시세 실패: {k} {e}")
    if missing:
        probe("한국 시세(네이버 보충)", f"{naver_ok}건 성공 / 야후에서 빠진 {len(missing)}종목")
    return uni, prices, index


# ───────────────────────── 미국 (위키백과 목록 + 야후) ─────────────────────────
def _wiki_table(url, need, rows=(0, 10 ** 6)):
    """열 이름에 need 중 하나가 들어 있고 행 수가 rows 범위인 첫 표."""
    seen = []
    for t in pd.read_html(io.StringIO(get(url))):
        cols = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
        seen.append(cols[:6])
        if rows[0] <= len(t) <= rows[1] and any(n in c.lower() for c in cols for n in need):
            t.columns = cols
            return t
    raise RuntimeError(f"표를 찾지 못함. 본 표의 열: {seen[:6]}")


def us_universe():
    sp = _wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("symbol",), rows=(400, 600))
    uni = pd.DataFrame({"ticker": sp["Symbol"], "name": sp["Security"],
                        "market": "S&P500", "sector": sp["GICS Sub-Industry"]})
    try:
        nd = _wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100", ("ticker", "symbol"), rows=(90, 115))
        tcol = next(c for c in nd.columns if "ticker" in c.lower() or "symbol" in c.lower())
        ncol = next((c for c in nd.columns if "company" in c.lower() or "security" in c.lower()), tcol)
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


def yahoo_prices(tickers):
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
    prices = yahoo_prices(list(uni["ticker"]) + ["SPY", "QQQ"])
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
        m = metrics(prices.get(str(u["ticker"])))
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
    scope = ("한국: 코스피 시총 상위 200 + 코스닥 시총 상위 150 (우선주·ETF·스팩·바이오 제외), 시세는 야후" if kr
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
    L += ["", "## 강해지는 업종 (5거래일 사이 지수 대비 강도가 가장 많이 오른 순 · 20일선 위 비율이 낮으면 급락 뒤 반등일 수 있음)"] + sector_rows(rising)
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
    with open(os.path.join(a.out, f"probe_{a.market}.md"), "w", encoding="utf-8") as f:
        f.write(f"조달 진단 {now:%Y-%m-%d %H:%M} KST ({a.market}, 상태 {status})\n\n" + "\n".join(PROBE + [f"- 막힘: {p}" for p in PROBLEMS[:40]]) + "\n")
    print(f"{a.market}: 상태 {status}, 계산 {len(df)}종목, 막힌 곳 {len(PROBLEMS)}건")


if __name__ == "__main__":
    main()
