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


def quarterize(durations, direct_only=False):
    """{(start,end): val} → {end: 분기값}. 직접 분기값 우선, 없으면 누적값 차감으로 계산."""
    quarters = {}
    # 1) 직접 분기값 (75~105일)
    for (s, e), v in durations.items():
        if 75 <= _days(s, e) <= 105:
            quarters[e] = v
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
                    quarters[e] = v - v2
                    break
    # 3) 연간값 - 그 해 분기 3개 합 (손익계산서는 분기값만 공시하는 경우가 많음)
    for (s, e), v in durations.items():
        if e in quarters or not (350 <= _days(s, e) <= 380):
            continue
        inside = sorted(q for q in quarters if _d(s) < _d(q) < _d(e))
        if len(inside) == 3:
            first_gap = (_d(inside[0]) - _d(s)).days
            if 75 <= first_gap <= 105:
                quarters[e] = v - sum(quarters[q] for q in inside)
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


def build_series(compacted):
    """매출 분기 축에 맞춰 모든 개념을 정렬한 시계열을 만든다."""
    dur = {name: _combine(compacted, tags, how) for name, (tags, how) in DURATION_CONCEPTS.items()}
    inst = {name: _combine(compacted, tags, how, instant=True) for name, (tags, how) in INSTANT_CONCEPTS.items()}

    rev_q = quarterize(dur["revenue"])
    spine = consecutive_tail(rev_q)
    ends = [e for e, _ in spine]

    series = {"end": ends, "revenue": [v for _, v in spine]}
    for name in ["cogs", "gross_profit", "op_income", "ocf", "capex", "da", "net_income"]:
        qm = quarterize(dur[name])
        series[name] = [_match(qm, e) for e in ends]
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
