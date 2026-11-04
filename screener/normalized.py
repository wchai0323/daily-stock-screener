"""정상화 PER: 시가총액 ÷ 최근 최대 10개 회계연도의 평균 순이익.

경기 순환 업종은 이익이 정점일 때 TTM PER이 가장 싸 보인다. 사이클 전체의 평균 이익으로 나누면
그 착시를 줄일 수 있다 (그레이엄·쉴러 방식). 순이익은 지배주주 귀속 기준, 물가 조정은 하지 않는다.
"""
import datetime as dt

MIN_YEARS = 5
MAX_YEARS = 10
NI_TAGS = ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss")


def annual_net_income_sec(compacted):
    """SEC 압축본 → {회계연도 종료일: 연간 순이익}. 기간 340~390일인 값만, 같은 기간은 최신 공시값."""
    for tag in NI_TAGS:
        rows = (compacted or {}).get("concepts", {}).get(tag) or []
        best = {}
        for start, end, val, filed, _accn in rows:
            if not start or not end or val is None:
                continue
            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
            if not 340 <= days <= 390:
                continue
            if end not in best or (filed or "") >= best[end][1]:
                best[end] = (val, filed or "")
        if best:
            # 회계연도가 바뀐 해에 종료일이 몇 주 차이로 겹치면 같은 연도로 보고 최근 것만
            by_year = {}
            for end in sorted(best):
                by_year[end[:4]] = (end, best[end][0])
            return {end: v for end, v in by_year.values()}
    return {}


def compute(mkt_cap, annual):
    """{기간: 순이익} → (정상화 PER, 사용 연수, 평균 순이익). 연수 부족이면 PER None, 평균 적자면 PER None."""
    if not annual:
        return None, 0, None
    vals = [annual[k] for k in sorted(annual)][-MAX_YEARS:]
    n = len(vals)
    if n < MIN_YEARS:
        return None, n, None
    avg = sum(vals) / n
    pe = (mkt_cap / avg) if (mkt_cap and avg > 0) else None
    return pe, n, avg
