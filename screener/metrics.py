"""분기 시계열 → 스크리닝 지표. 모든 비율은 소수(0.25 = 25%)."""


def _sum(vals):
    if any(v is None for v in vals):
        return None
    return sum(vals)


def _ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def _growth(cur, prev):
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1


def _sub(a, b):
    if a is None or b is None:
        return None
    return a - b


def compute(series):
    """series: facts.build_series 결과. 분기 8개 미만이면 None."""
    n = len(series["end"])
    if n < 8:
        return None
    m = {"quarters": n, "period_end": series["end"][-1]}
    last4 = slice(n - 4, n)
    prev4 = slice(n - 8, n - 4)

    def ttm(name, sl=last4):
        return _sum(series[name][sl])

    rev, rev_p = ttm("revenue"), ttm("revenue", prev4)
    gp, gp_p = ttm("gross_profit"), ttm("gross_profit", prev4)
    op, op_p = ttm("op_income"), ttm("op_income", prev4)
    ocf, capex, da = ttm("ocf"), ttm("capex"), ttm("da")

    m["revenue_ttm"] = rev
    m["rev_growth"] = _growth(rev, rev_p)
    m["gross_profit_ttm"] = gp
    m["gpm"] = _ratio(gp, rev)
    m["gpm_prev"] = _ratio(gp_p, rev_p)
    m["gp_growth"] = _growth(gp, gp_p)
    m["op_income_ttm"] = op
    m["net_income_ttm"] = ttm("net_income") if "net_income" in series else None
    m["opm"] = _ratio(op, rev)
    m["opm_prev"] = _ratio(op_p, rev_p)
    m["opm_change"] = _sub(m["opm"], m["opm_prev"])

    # 분기 YoY와 성장 가속 (최근 분기 YoY - 직전 2개 분기 YoY 평균)
    yoy = []
    for i in range(3):
        cur, prev = series["revenue"][n - 1 - i], series["revenue"][n - 5 - i] if n - 5 - i >= 0 else None
        yoy.append(_growth(cur, prev))
    m["q_yoy"] = yoy[0]
    m["accel"] = (yoy[0] - (yoy[1] + yoy[2]) / 2) if None not in yoy else None

    # 현금흐름: 설비투자 태그가 없으면 0으로 보고 표시
    m["capex_missing"] = capex is None
    fcf = _sub(ocf, capex if capex is not None else 0) if ocf is not None else None
    m["fcf_ttm"] = fcf
    m["fcf_margin"] = _ratio(fcf, rev)
    m["da_missing"] = da is None
    m["ebitda_ttm"] = (op + (da or 0)) if op is not None else None

    # 재무상태표 (최근 분기말, 4분기 전)
    def at(name, idx):
        return series[name][idx]

    def liquid(idx):
        cash = at("cash", idx)
        if cash is None:
            return None
        return cash + (at("st_investments", idx) or 0)

    def debt(idx):
        lt = at("lt_debt", idx) or 0
        cur = at("lt_debt_current", idx) or 0
        st = at("st_borrowings", idx) or 0
        return lt + cur + st

    last, year_ago = n - 1, n - 5
    m["liquid"] = liquid(last)
    m["debt"] = debt(last)
    m["net_debt"] = _sub(m["debt"], m["liquid"])
    m["assets"] = at("assets", last)
    op_assets = _sub(at("assets", last), liquid(last))
    op_assets_prev = _sub(at("assets", year_ago), liquid(year_ago))
    m["op_asset_growth"] = _growth(op_assets, op_assets_prev)
    m["gp_to_assets"] = _ratio(gp, at("assets", last))

    # 희석: 최근 분기 희석 가중평균 주식 수 vs 4분기 전
    m["dilution"] = _growth(series["diluted_shares"][last], series["diluted_shares"][year_ago])
    m["diluted_shares_last"] = series["diluted_shares"][last]

    m["profitable"] = op is not None and op > 0
    if m["profitable"] and m["ebitda_ttm"] and m["ebitda_ttm"] > 0 and m["net_debt"] is not None:
        m["net_debt_ebitda"] = m["net_debt"] / m["ebitda_ttm"]
    else:
        m["net_debt_ebitda"] = None
    if fcf is not None and fcf < 0 and m["liquid"] is not None:
        m["runway_months"] = m["liquid"] / (-fcf) * 12
    elif fcf is not None and fcf >= 0:
        m["runway_months"] = float("inf")
    else:
        m["runway_months"] = None
    return m
