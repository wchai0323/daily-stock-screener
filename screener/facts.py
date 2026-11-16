"""XBRL companyfacts에서 필요한 개념만 뽑아 분기 시계열로 만든다.

SEC 원본은 기업당 수 MB라서, 필요한 태그만 남긴 '압축본'을 캐시에 저장하고
분기 계산은 압축본으로 한다.
"""
import datetime as dt

# 개념별 후보 태그 (앞쪽이 우선). 'max'는 같은 기간에 여러 태그가 있으면 최댓값 사용.
COMPACT_VERSION = 2   # 2: 순이익(net_income) 태그 추가

DURATION_CONCEPTS = {
    "net_income": (["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"], "first"),
    "revenue": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                 "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense"], "max"),
    "cogs": (["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
              "CostOfServices", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"], "first"),
    "gross_profit": (["GrossProfit"], "first"),
    "op_income": (["OperatingIncomeLoss"], "first"),
    "ocf": (["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "first"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
               "PaymentsForCapitalImprovements"], "first"),
    "da": (["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet", "Depreciation"], "first"),
    "diluted_shares": (["WeightedAverageNumberOfDilutedSharesOutstanding",
                        "WeightedAverageNumberOfSharesOutstandingBasic"], "first"),
}
INSTANT_CONCEPTS = {
    "assets": (["Assets"], "first"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue",
              "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"], "first"),
    "st_investments": (["ShortTermInvestments", "MarketableSecuritiesCurrent",
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent"], "first"),
    "lt_debt": (["LongTermDebt", "LongTermDebtNoncurrent"], "first"),
    "lt_debt_current": (["LongTermDebtCurrent"], "first"),
    "st_borrowings": (["ShortTermBorrowings", "CommercialPaper"], "first"),
}
SHARES_OUT_TAG = "EntityCommonStockSharesOutstanding"
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT"}


def _d(s):
    return dt.date.fromisoformat(s)


def compact(facts_json):
    """companyfacts 원본 → {'concepts': {tag: [[start, end, val, filed, accn]...]}, 'shares_out': [...]}"""
    out = {"name": facts_json.get("entityName"), "concepts": {}, "shares_out": [], "version": COMPACT_VERSION}
    gaap = facts_json.get("facts", {}).get("us-gaap", {})
    wanted = set()
    for tags, _ in list(DURATION_CONCEPTS.values()) + list(INSTANT_CONCEPTS.values()):
        wanted.update(tags)
    for tag in wanted:
        node = gaap.get(tag)
        if not node:
            continue
        unit = "USD" if "USD" in node.get("units", {}) else ("shares" if "shares" in node.get("units", {}) else None)
        if unit is None:
            continue
        rows = []
        for f in node["units"][unit]:
            if f.get("form") not in FORMS:
                continue
            rows.append([f.get("start"), f["end"], f["val"], f.get("filed"), f.get("accn")])
        if rows:
            out["concepts"][tag] = rows
    dei = facts_json.get("facts", {}).get("dei", {}).get(SHARES_OUT_TAG)
    if dei:
        for f in dei.get("units", {}).get("shares", []):
            out["shares_out"].append([f["end"], f["val"], f.get("filed"), f.get("accn")])
    return out


# ---------------- 분기화 ----------------

def _latest_by_period(rows, instant=False):
    """같은 기간(start,end) 값이 여러 번 공시되면 가장 최근 공시값을 쓴다."""
    best = {}
    for start, end, val, filed, _accn in rows:
        key = end if instant else (start, end)
        if key is None or (not instant and start is None):
            continue
        if key not in best or (filed or "") >= best[key][1]:
            best[key] = (val, filed or "")
    return {k: v[0] for k, v in best.items()}


REVISION_MIN = 0.03      # 같은 기간 값이 이 비율 넘게 바뀌면 재작성으로 본다 (중단사업 분류 등)
REVISION_EVENT_DAYS = 400   # 한 번의 재작성이 이후 공시들에 걸쳐 반영되는 기간


def _changed(a, b):
    return abs(a - b) > REVISION_MIN * max(abs(a), abs(b), 1)


def _period_meta(hist):
    """{기간: [(공시일, 값)...]} → 기간별 (지금 값이 처음 공시된 날, 최근 공시일, 재작성 여부)."""
    out = {}
    for k, lst in hist.items():
        lst.sort(key=lambda x: x[0])
        latest = lst[-1][1]
        first, revised = lst[-1][0], False
        for i, (f, v) in enumerate(lst):
            if not _changed(v, latest):
                first, revised = f, any(_changed(v0, latest) for _, v0 in lst[:i])
                break
        out[k] = (first, lst[-1][0], revised)
    return out


def _combined_history(compacted, tags, how):
    """기간·공시별로 태그 값을 _combine과 같은 규칙으로 합친 이력 (태그만 바꿔 공시한 것은 재작성으로 보지 않게)."""
    by = {}
    for rank, t in enumerate(tags):
        for start, end, val, filed, _accn in compacted["concepts"].get(t, []):
            if start is None or end is None or val is None:
                continue
            by.setdefault((start, end), {}).setdefault(filed or "", []).append((rank, val))
    hist = {}
    for k, filings in by.items():
        for f, vals in filings.items():
            v = max(x[1] for x in vals) if how == "max" else min(vals)[1]
            hist.setdefault(k, []).append((f, v))
    return hist


def _combine_meta(compacted, tags, how="first"):
    """값마다 유효한 '기준' 구간을 계산한다.

    기준 = 그 시점까지 반영된 재작성 횟수. 재작성일들은 REVISION_EVENT_DAYS 안에서 한 번으로 묶는다
    (중단사업 분류처럼 연간 값은 10-K에서, 분기 값은 이후 10-Q들에서 차례로 재작성되는 경우).
    값이 처음 공시된 날 ~ 마지막으로 다시 공시된 날의 기준 구간에서, 차감하는 값끼리 구간이 겹쳐야 같은 기준이다.
    """
    span, revs = {}, set()
    for k, (first, last, revised) in _period_meta(_combined_history(compacted, tags, how)).items():
        span[k] = (first, last)
        if revised:
            revs.add(first)
    events = []
    for r in sorted(revs):
        if not events or (_d(r) - _d(events[-1][1])).days > REVISION_EVENT_DAYS:
            events.append([r, r])
        else:
            events[-1][1] = r
    starts = [e[0] for e in events]

    def basis(f):
        return sum(1 for ev in starts if f and f >= ev)

    return {k: (basis(first), basis(last)) for k, (first, last) in span.items()}


def _plausible(q, ref):
    """기준이 어긋나 보여도 차감한 분기값이 같은 해 다른 분기 평균의 0.6~1.67배면 받아들인다.
    (분기 값이 먼저 새 기준으로 공시되고 연간 값이 나중에 재작성되는 경우 등)"""
    return ref is not None and ref > 0 and q is not None and 0.6 <= q / ref <= 1 / 0.6


def _overlap(intervals):
    lo = max(i[0] for i in intervals)
    hi = min(i[1] for i in intervals)
    return (lo, hi) if lo <= hi else None


def _combine(compacted, tags, how, instant=False):
    maps = [_latest_by_period(compacted["concepts"].get(t, []), instant) for t in tags]
    keys = set().union(*[m.keys() for m in maps]) if maps else set()
    out = {}
    for k in keys:
        vals = [m[k] for m in maps if k in m]
        if not vals:
            continue
        out[k] = max(vals) if how == "max" else vals[0]
    return out


def _days(s, e):
    return (_d(e) - _d(s)).days


def quarterize(durations, direct_only=False, meta=None):
    """{(start,end): val} → {end: 분기값}. 직접 분기값 우선, 없으면 누적값 차감으로 계산.

    meta({(start,end): 기준 구간}, _combine_meta 결과)를 주면, 재작성된 연간·누적값에서 재작성되지 않은
    예전 값을 빼는 경우(기준 불일치)를 막는다. 그런 분기는 값을 None으로 둔다 (날짜는 남겨 분기 축이 끊기지 않게).
    """
    meta = meta or {}
    anyb = (0, 10 ** 6)

    def basis(key):
        return meta.get(key, anyb)

    quarters, qbasis = {}, {}
    # 1) 직접 분기값 (75~105일)
    for (s, e), v in durations.items():
        if 75 <= _days(s, e) <= 105:
            quarters[e] = v
            qbasis[e] = basis((s, e))
    if direct_only:
        return quarters
    # 2) 누적(YTD·연간) 차감: 같은 시작일의 더 짧은 누적값과의 차이
    by_start = {}
    for (s, e), v in durations.items():
        by_start.setdefault(s, []).append((e, v))
    for s, lst in by_start.items():
        lst.sort()
        for i, (e, v) in enumerate(lst):
            if e in quarters:
                continue
            length = _days(s, e)
            if length < 150:
                continue
            for e2, v2 in lst[:i]:
                gap = (_d(e) - _d(e2)).days
                if 75 <= gap <= 105:
                    ov = _overlap([basis((s, e)), basis((s, e2))])
                    q = v - v2
                    if ov is None and not _plausible(q, v2 / max(round(_days(s, e2) / 91), 1)):
                        continue   # 기준 불일치 (한쪽만 재작성)
                    quarters[e] = q
                    qbasis[e] = ov or basis((s, e))
                    break
    # 3) 연간값 - 그 해 분기 3개 합 (손익계산서는 분기값만 공시하는 경우가 많음)
    for (s, e), v in durations.items():
        if e in quarters or not (350 <= _days(s, e) <= 380):
            continue
        inside = sorted(q for q in quarters if _d(s) < _d(q) < _d(e))
        if len(inside) == 3 and all(quarters[q] is not None for q in inside):
            first_gap = (_d(inside[0]) - _d(s)).days
            if 75 <= first_gap <= 105:
                ov = _overlap([basis((s, e))] + [qbasis.get(q, anyb) for q in inside])
                q4 = v - sum(quarters[q] for q in inside)
                if ov is not None or _plausible(q4, sum(quarters[q] for q in inside) / 3):
                    quarters[e] = q4
                    qbasis[e] = ov or basis((s, e))
                else:
                    quarters[e] = None   # 기준 불일치: 값을 만들지 않음
    return quarters


def consecutive_tail(qmap, max_len=12):
    """가장 최근 분기부터 거꾸로, 간격 75~105일로 이어지는 분기만 남긴다."""
    ends = sorted(qmap, reverse=True)
    if not ends:
        return []
    chain = [ends[0]]
    for e in ends[1:]:
        gap = (_d(chain[-1]) - _d(e)).days
        if gap < 75:
            continue  # 중복에 가까운 기간 (회계연도 변경 등)
        if gap > 105:
            break
        chain.append(e)
        if len(chain) >= max_len:
            break
    return [(e, qmap[e]) for e in reversed(chain)]


def _match(qmap, end, tol=12):
    """분기말 날짜가 하루이틀 어긋나는 경우를 허용해 값을 찾는다."""
    if end in qmap:
        return qmap[end]
    t = _d(end)
    for k, v in qmap.items():
        if abs((_d(k) - t).days) <= tol:
            return v
    return None


def annual_values(compacted, concept="revenue", filed=None):
    """최근 10-K가 같이 보여주는 연간 값 [(종료일, 값)...] (보통 3개 연도).

    같은 공시에 실린 연도끼리만 써서 중단사업 재작성 전후 값이 섞이지 않게 한다.
    filed를 주면 그 공시일 기준 (매출과 영업이익을 같은 10-K에서 읽을 때).
    """
    tags, how = DURATION_CONCEPTS[concept]
    hist = _combined_history(compacted, tags, how)
    annual = {k: lst for k, lst in hist.items() if 350 <= _days(*k) <= 380}
    if not annual:
        return []
    last = filed or max(f for lst in annual.values() for f, _ in lst)
    rows = []
    for (s, e), lst in annual.items():
        vals = [v for f, v in lst if f == last]
        if vals:
            rows.append((e, vals[0]))
    return sorted(rows)


def annual_revenue(compacted):
    return annual_values(compacted, "revenue")


def annual_latest_filed(compacted):
    tags, how = DURATION_CONCEPTS["revenue"]
    fs = [f for k, lst in _combined_history(compacted, tags, how).items() if 350 <= _days(*k) <= 380 for f, _ in lst]
    return max(fs) if fs else None


def build_series(compacted):
    """매출 분기 축에 맞춰 모든 개념을 정렬한 시계열을 만든다."""
    dur = {name: _combine(compacted, tags, how) for name, (tags, how) in DURATION_CONCEPTS.items()}
    inst = {name: _combine(compacted, tags, how, instant=True) for name, (tags, how) in INSTANT_CONCEPTS.items()}

    # 기준 불일치(재작성) 검사는 매출로만 한다. 매출은 중단사업 분류 때 크게 바뀌고 평소엔 거의 안 바뀐다.
    # 현금흐름·영업이익은 평소 재분류로도 값이 자주 바뀌어 같은 검사를 하면 멀쩡한 분기까지 지워진다
    rev_meta = _combine_meta(compacted, *DURATION_CONCEPTS["revenue"])
    rev_q = quarterize(dur["revenue"], meta=rev_meta)
    spine = consecutive_tail(rev_q)
    ends = [e for e, _ in spine]

    series = {"end": ends, "revenue": [v for _, v in spine], "annual_revenue": annual_revenue(compacted),
              "annual_op_income": annual_values(compacted, "op_income", annual_latest_filed(compacted))}
    for name in ["cogs", "gross_profit", "op_income", "ocf", "capex", "da", "net_income"]:
        qm = quarterize(dur[name])
        # 매출이 기준 불일치로 빠진 분기는 다른 항목도 같은 이유로 틀리므로 비운다
        series[name] = [None if r is None else _match(qm, e) for e, r in zip(ends, series["revenue"])]
    shares_q = quarterize(dur["diluted_shares"], direct_only=True)
    series["diluted_shares"] = [_match(shares_q, e) for e in ends]
    # 4분기(연간 보고)는 희석 주식 수 직접 분기값이 없는 경우가 많아 연간 가중평균으로 보완
    annual_shares = {e: v for (s, e), v in dur["diluted_shares"].items() if 350 <= _days(s, e) <= 380}
    for i, e in enumerate(ends):
        if series["diluted_shares"][i] is None:
            series["diluted_shares"][i] = _match(annual_shares, e)
    for name, im in inst.items():
        series[name] = [_match(im, e) for e in ends]

    # 매출총이익: 태그가 없으면 매출 - 매출원가
    gp = []
    for r, c, g in zip(series["revenue"], series["cogs"], series["gross_profit"]):
        gp.append(g if g is not None else (r - c if (r is not None and c is not None) else None))
    series["gross_profit"] = gp
    return series


def shares_outstanding(compacted):
    """표지(dei)의 최신 발행주식수. 클래스가 여러 개면 같은 공시의 값을 합산."""
    rows = compacted.get("shares_out") or []
    if not rows:
        return None
    latest_filed = max((r[2] or "") for r in rows)
    same = [r for r in rows if (r[2] or "") == latest_filed]
    latest_end = max(r[0] for r in same)
    seen = set()
    total = 0
    for end, val, _filed, accn in same:
        if end != latest_end or (accn, val) in seen:
            continue
        seen.add((accn, val))
        total += val
    return total or None


def latest_filed(compacted):
    filed = [r[3] for rows in compacted["concepts"].values() for r in rows if r[3]]
    return max(filed) if filed else None
