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


def fetch_quote_info(tickers, pause=0.4):
    """1층 통과 종목만: 공매도 비율, PER(TTM·Forward), PEG, 섹터·업종 (종목당 1회 호출)."""
    import yfinance as yf

    out = {}
    for t in tickers:
        try:
            out[t] = parse_info(yf.Ticker(t).info)
        except Exception:  # noqa: BLE001 - 조회 실패는 빈 값으로
            out[t] = parse_info({})
        time.sleep(pause)
    return out
