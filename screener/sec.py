"""SEC EDGAR 접근: 종목 목록, 기업 메타데이터, XBRL 재무(companyfacts), 일별 공시 인덱스.

SEC는 연락처가 들어간 User-Agent를 요구하고 초당 10건으로 요청을 제한한다.
User-Agent는 SEC_USER_AGENT 환경변수(GitHub 시크릿)로 받는다.
"""
import datetime as dt
import os
import time

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"


class SecClient:
    def __init__(self, user_agent=None, min_interval=0.125):
        ua = user_agent or os.environ.get("SEC_USER_AGENT")
        if not ua:
            raise RuntimeError(
                "SEC_USER_AGENT 환경변수가 필요합니다. 예: 'Daily Stock Screener your@email.com'"
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last = 0.0
        self.request_count = 0

    def get(self, url, retries=5):
        for attempt in range(retries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.request_count += 1
            try:
                r = self.session.get(url, timeout=60)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            # 429/403/5xx: 잠시 쉬고 재시도
            time.sleep(min(60, 2 ** (attempt + 1)))
        return None

    # ---- 종목 목록 ----
    def tickers(self):
        r = self.get(TICKERS_URL)
        if r is None:
            raise RuntimeError("SEC 종목 목록을 받지 못했습니다")
        js = r.json()
        fields = js["fields"]
        rows = [dict(zip(fields, row)) for row in js["data"]]
        return rows

    def submissions(self, cik):
        r = self.get(SUBMISSIONS_URL.format(cik=int(cik)))
        return r.json() if r is not None else None

    def companyfacts(self, cik):
        r = self.get(FACTS_URL.format(cik=int(cik)))
        return r.json() if r is not None else None

    def daily_index(self, day: dt.date):
        """해당 일자의 공시 목록에서 (form, cik) 목록을 돌려준다. 주말·휴일은 빈 목록."""
        q = (day.month - 1) // 3 + 1
        url = DAILY_INDEX_URL.format(y=day.year, q=q, ymd=day.strftime("%Y%m%d"))
        r = self.get(url, retries=3)
        if r is None:
            return []
        return parse_form_index(r.text)


def parse_form_index(text):
    """form.YYYYMMDD.idx (고정폭) 파싱. 'CIK' 열 앞뒤 공백 구분에 의존하지 않고 끝에서부터 자른다."""
    out = []
    started = False
    for line in text.splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started or not line.strip():
            continue
        # 마지막 3개 토큰: CIK, 날짜, 파일명 / 첫 토큰(들): form type
        parts = line.split()
        if len(parts) < 4:
            continue
        filename, date_str, cik = parts[-1], parts[-2], parts[-3]
        form = parts[0]
        if parts[0] in ("10-Q", "10-K") and len(parts) > 1 and parts[1] == "/A":
            form = parts[0] + "/A"
        if not cik.isdigit():
            continue
        out.append({"form": form, "cik": int(cik), "date": date_str, "file": filename})
    return out
