"""Yahoo Finance(yfinance): 1년 일봉 일괄 다운로드와 공매도 비율 조회."""
import time

import pandas as pd


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def summarize(df):
    """한 종목의 일봉 DataFrame → 가격 요약. 데이터가 부족하면 None."""
    df = df.dropna(subset=["Close"])
    if len(df) < 20:
        return None
    close, vol = df["Close"], df["Volume"].fillna(0)
    return {
        "price": float(close.iloc[-1]),
        "price_date": str(df.index[-1].date()),
        "adv20": float((close * vol).tail(20).mean()),
        "hi52": float(df["High"].max()),
        "lo52": float(df["Low"].min()),
        "ma200": float(close.tail(200).mean()) if len(close) >= 200 else None,
    }


def fetch_prices(tickers, chunk_size=150, pause=1.0):
    import yfinance as yf

    out = {}
    for chunk in _chunks(list(tickers), chunk_size):
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception:  # noqa: BLE001 - 네트워크 오류 시 재시도
                time.sleep(5 * (attempt + 1))
        else:
            continue
        if df is None or df.empty:
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in chunk:
            try:
                sub = df[t] if multi else df
            except KeyError:
                continue
            s = summarize(sub)
            if s:
                out[t] = s
        time.sleep(pause)
    return out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def parse_info(info):
    """yfinance .info → 메일·점수에 쓰는 필드만. 값이 없거나 이상하면 None."""
    info = info or {}
    peg = _num(info.get("trailingPegRatio"))
    if peg is None:
        peg = _num(info.get("pegRatio"))
    return {
        "short_pct_float": _num(info.get("shortPercentOfFloat")),
        "pe_ttm": _num(info.get("trailingPE")),
        "pe_fwd": _num(info.get("forwardPE")),
        "peg": peg,
        "sector": info.get("sector") or None,
        "industry": info.get("industry") or None,
    }


QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{t}"
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QS_MODULES = "assetProfile,summaryDetail,defaultKeyStatistics,financialData"
FIELDS = ("short_pct_float", "pe_ttm", "pe_fwd", "peg", "sector", "industry")


def _raw(v):
    return v.get("raw") if isinstance(v, dict) else v


def parse_quote_summary(js):
    """quoteSummary 원본 JSON → yfinance .info와 같은 평평한 dict."""
    res = ((js or {}).get("quoteSummary") or {}).get("result") or []
    flat = {}
    for module in (res[0] if res else {}).values():
        if isinstance(module, dict):
            for k, v in module.items():
                v = _raw(v)
                if v is not None and k not in flat:
                    flat[k] = v
    return flat


def parse_quote_batch(js):
    """v7 quote 원본 JSON → {ticker: 평평한 dict}."""
    rows = ((js or {}).get("quoteResponse") or {}).get("result") or []
    return {r.get("symbol"): {k: _raw(v) for k, v in r.items()} for r in rows if r.get("symbol")}


def _merge(base, extra):
    for k in FIELDS:
        if base.get(k) is None and extra.get(k) is not None:
            base[k] = extra[k]
    return base


def _err(diag, stage, e):
    msg = f"{type(e).__name__}: {str(e)[:150]}"
    diag["errors"].setdefault(stage, {"count": 0, "first": msg})["count"] += 1


def fetch_quote_info(tickers, pause=0.5, retries=2):
    """1층 통과 종목만: 공매도 비율, PER(TTM·Forward), PEG, 섹터·업종.

    Yahoo가 GitHub 서버 요청을 자주 막아서 세 경로를 차례로 시도한다.
      1) v7 quote 일괄 조회 (PER·Forward PER)
      2) 종목별 quoteSummary (섹터·업종·PEG·공매도·PER)
      3) yfinance .info (위 둘이 비었을 때)
    반환: ({ticker: dict}, 진단 dict) — 진단은 실행 요약에 남긴다.
    """
    import yfinance as yf
    from yfinance.data import YfData

    data = YfData()
    out = {t: parse_info({}) for t in tickers}
    diag = {"tickers": len(tickers), "errors": {}}

    # 1) 일괄 조회
    for i in range(0, len(tickers), 50):
        chunk = list(tickers)[i:i + 50]
        for attempt in range(retries + 1):
            try:
                js = data.get_raw_json(QUOTE_URL, params={"symbols": ",".join(chunk), "formatted": "false"})
                for t, info in parse_quote_batch(js).items():
                    if t in out:
                        _merge(out[t], parse_info(info))
                break
            except Exception as e:  # noqa: BLE001
                if attempt == retries:
                    _err(diag, "v7_quote", e)
                time.sleep(2 * (attempt + 1))

    # 2) 종목별 상세 → 3) .info
    for t in tickers:
        if all(out[t].get(k) is not None for k in ("sector", "pe_fwd")):
            continue
        got = False
        for attempt in range(retries + 1):
            try:
                js = data.get_raw_json(QS_URL.format(t=t), params={"modules": QS_MODULES, "formatted": "false"})
                _merge(out[t], parse_info(parse_quote_summary(js)))
                got = out[t].get("sector") is not None
                break
            except Exception as e:  # noqa: BLE001
                if attempt == retries:
                    _err(diag, "quoteSummary", e)
                time.sleep(2 * (attempt + 1))
        if not got:
            try:
                _merge(out[t], parse_info(yf.Ticker(t).info))
            except Exception as e:  # noqa: BLE001
                _err(diag, "info", e)
        time.sleep(pause)

    for k in FIELDS:
        diag[f"filled_{k}"] = sum(out[t].get(k) is not None for t in tickers)
    return out, diag
