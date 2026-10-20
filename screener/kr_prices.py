"""한국 주가: 네이버 금융 일별 시세(종목별), 실패한 종목만 Yahoo로 보완.

Yahoo는 한국 종목을 일괄로 요청하면 상당수가 빈 응답이라 1차 출처로 쓰지 않는다.
"""
import ast
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from . import prices

NAVER_URL = "https://api.finance.naver.com/siseJson.naver"
LAST_DIAG = {}


def parse_naver(text):
    """siseJson 응답(파이썬 리스트 모양의 텍스트) → OHLCV DataFrame (DatetimeIndex)."""
    rows = ast.literal_eval(text.strip())
    if not rows or len(rows) < 2:
        return None
    df = pd.DataFrame(rows[1:], columns=[str(c).strip() for c in rows[0]])
    df = df.rename(columns={"날짜": "Date", "시가": "Open", "고가": "High", "저가": "Low", "종가": "Close",
                            "거래량": "Volume"})
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str.strip(), format="%Y%m%d")
    for k in ("Open", "High", "Low", "Close", "Volume"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    return df.set_index("Date").sort_index()


def fetch(codes_to_ticker, today=None, workers=8):
    """{6자리 코드: 티커(005930.KS)} → {코드: 가격 요약}. 진단은 LAST_DIAG에."""
    today = today or dt.date.today()
    start = (today - dt.timedelta(days=380)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"})
    s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers))
    out, errs = {}, []

    def one(code):
        try:
            r = s.get(NAVER_URL, params={"symbol": code, "requestType": 1, "startTime": start, "endTime": end,
                                         "timeframe": "day"}, timeout=20)
            r.raise_for_status()
            df = parse_naver(r.text)
            return code, (prices.summarize(df) if df is not None else None), None
        except Exception as e:  # noqa: BLE001
            return code, None, f"{type(e).__name__}: {str(e)[:120]}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, summary, err in ex.map(one, list(codes_to_ticker)):
            if summary:
                out[code] = summary
            if err:
                errs.append(err)
    errors, first_error = len(errs), (errs[0] if errs else None)

    # 네이버에서 못 받은 종목만 Yahoo로
    missing = {c: t for c, t in codes_to_ticker.items() if c not in out}
    if len(missing) > 400:                  # 네이버가 통째로 막힌 날 Yahoo로 수천 건을 돌리지 않는다
        missing = dict(list(missing.items())[:400])
    yahoo = prices.fetch_prices(list(missing.values())) if missing else {}
    by_ticker = {t: c for c, t in missing.items()}
    for t, v in yahoo.items():
        out[by_ticker[t]] = v
    LAST_DIAG.clear()
    LAST_DIAG.update({"requested": len(codes_to_ticker), "naver_ok": len(out) - len(yahoo), "naver_errors": errors,
                      "naver_first_error": first_error, "yahoo_fallback_requested": len(missing),
                      "yahoo_fallback_ok": len(yahoo), "received": len(out)})
    return out
