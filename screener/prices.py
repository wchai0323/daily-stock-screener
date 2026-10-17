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


def fetch_short_interest(tickers, pause=0.4):
    """1층 통과 종목만 대상으로 공매도/유통주식 비율을 가져온다 (종목당 1회 호출)."""
    import yfinance as yf

    out = {}
    for t in tickers:
        try:
            info = yf.Ticker(t).info or {}
            v = info.get("shortPercentOfFloat")
            out[t] = float(v) if v is not None else None
        except Exception:  # noqa: BLE001
            out[t] = None
        time.sleep(pause)
    return out
