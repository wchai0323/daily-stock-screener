"""0단계 유니버스 → 1층 하드 필터 → 2층 자동 점수.

입력: 기업별 dict (metrics + meta + 시장 데이터). 출력: 같은 dict에 판정·사유·점수를 붙인 목록.
"""
import math

import pandas as pd

from . import config as C


def _excluded_sic(sic):
    if sic is None:
        return False
    if sic in C.EXCLUDED_SIC_CODES:
        return True
    return any(lo <= sic <= hi for lo, hi in C.EXCLUDED_SIC_RANGES)


def _pct_rank(values):
    """None은 제외하고 백분위(0~1)를 매긴다. 반환은 원래 순서의 리스트."""
    s = pd.Series(values, dtype="float64")
    return s.rank(pct=True).where(s.notna()).tolist()


def universe_reasons(c):
    """0단계 탈락 사유 목록 (빈 목록이면 통과)."""
    r = []
    if c.get("exchange") not in C.EXCHANGES:
        r.append("거래소")
    if _excluded_sic(c.get("sic")):
        r.append("금융·REIT·SPAC 업종")
    if c.get("country") in C.CHINA_LOCATION_CODES:
        r.append("H7 중국 연관(본사 주소)")
    if c.get("quarters", 0) < C.MIN_QUARTERS:
        r.append("분기 데이터 8개 미만")
    rev = c.get("revenue_ttm")
    if rev is None or rev < C.REVENUE_TTM_MIN:
        r.append("H3 매출 5천만 달러 미만")
    cap = c.get("mkt_cap")
    if cap is None:
        r.append("시가총액 확인 불가")
    elif cap < C.MKT_CAP_MIN:
        r.append("시가총액 3억 달러 미만")
    elif cap > C.MKT_CAP_MAX:
        r.append("H3 시가총액 200억 달러 초과")
    if (c.get("adv20") or 0) < C.ADV20_MIN:
        r.append("거래대금 부족")
    if (c.get("price") or 0) < C.PRICE_MIN:
        r.append("주가 3달러 미만")
    return r


def _industry_groups(rows):
    """SIC 3자리 → 2자리 → 전체 순으로 표본이 8개 이상인 비교 그룹을 정한다."""
    cnt3, cnt2 = {}, {}
    for c in rows:
        sic = c.get("sic")
        if sic is None:
            continue
        cnt3[sic // 10] = cnt3.get(sic // 10, 0) + 1
        cnt2[sic // 100] = cnt2.get(sic // 100, 0) + 1
    for c in rows:
        sic = c.get("sic")
        if sic is not None and cnt3.get(sic // 10, 0) >= C.INDUSTRY_MIN_PEERS:
            c["ind_group"] = f"SIC{sic // 10:03d}"
        elif sic is not None and cnt2.get(sic // 100, 0) >= C.INDUSTRY_MIN_PEERS:
            c["ind_group"] = f"SIC{sic // 100:02d}"
        else:
            c["ind_group"] = "ALL"


def _group_stats(rows, key):
    """그룹별 지표 분포: 중앙값, 70백분위, 그룹 내 백분위."""
    df = pd.DataFrame({"g": [c["ind_group"] for c in rows], "v": [c.get(key) for c in rows]})
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    med = df.groupby("g")["v"].median().to_dict()
    p70 = df.groupby("g")["v"].quantile(C.G_GPM_UNPROFITABLE_PCTL).to_dict()
    pct = df.groupby("g")["v"].rank(pct=True).tolist()
    return med, p70, pct


def hard_filter(c):
    """1층. 반환: (통과 여부, 차선 'G'/'C'/None, 탈락 사유 목록)"""
    reasons = []
    g = lambda k: c.get(k)

    # H1 희석
    if g("dilution") is None:
        reasons.append("H1 희석 데이터 없음")
    elif g("dilution") > C.H1_DILUTION_MAX:
        reasons.append(f"H1 희석 {g('dilution'):+.1%}")

    # H2 재무 생존력
    if g("profitable"):
        nde = g("net_debt_ebitda")
        if g("net_debt") is not None and g("net_debt") <= 0:
            pass  # 순현금
        elif nde is None or nde > C.H2_NET_DEBT_EBITDA_MAX:
            reasons.append("H2 순부채/EBITDA 2배 초과" if nde is not None else "H2 부채 판정 불가")
    else:
        runway = g("runway_months")
        if runway is None:
            reasons.append("H2 런웨이 판정 불가")
        elif runway < C.H2_RUNWAY_MONTHS_MIN:
            reasons.append(f"H2 런웨이 {runway:.0f}개월")

    # H5 블랙홀: 적자면 영업이익률 1년 +5%p 이상 개선
    if not g("profitable"):
        ch = g("opm_change")
        if ch is None or ch < C.H5_OPM_IMPROVEMENT_MIN:
            reasons.append("H5 적자 + 마진 개선 부족")

    # H6 투자 효율: 영업자산 증가율 <= 매출총이익 증가율
    oa, gpg = g("op_asset_growth"), g("gp_growth")
    if oa is None or gpg is None:
        reasons.append("H6 판정 불가")
    elif oa > gpg:
        reasons.append(f"H6 자산 증가({oa:+.0%}) > 매출총이익 증가({gpg:+.0%})")

    # H4 성장 차선
    lane, lane_reasons = _lane(c)
    if lane is None:
        reasons.extend(lane_reasons)
    return (not reasons), lane, reasons


def _lane(c):
    g = lambda k: c.get(k)
    rg, gpm = g("rev_growth"), g("gpm")
    why_g, why_c = [], []

    # G 차선
    if rg is None or rg < C.G_REV_GROWTH_MIN:
        why_g.append("성장 25% 미만")
    if g("accel") is None or g("accel") <= C.G_ACCEL_FLOOR:
        why_g.append("성장 급감속")
    if gpm is None:
        why_g.append("매출총이익률 없음")
    else:
        if g("profitable"):
            floor = max(C.G_GPM_FLOOR_PROFITABLE, g("ind_gpm_median") or 0)
        else:
            floor = max(C.G_GPM_FLOOR_UNPROFITABLE, g("ind_gpm_p70") or 0)
        if gpm < floor:
            why_g.append(f"매출총이익률 {gpm:.0%} < 기준 {floor:.0%}")
        if g("gpm_prev") is not None and g("gpm_prev") - gpm > C.G_GPM_DECLINE_MAX:
            why_g.append("매출총이익률 2%p 초과 하락")
    if not why_g:
        return "G", []

    # C 차선
    if rg is None or rg < C.C_REV_GROWTH_MIN:
        why_c.append("성장 10% 미만")
    if g("fcf_yield_pct") is None or g("fcf_yield_pct") < C.C_FCF_YIELD_PCTL:
        why_c.append("FCF 수익률 상위 30% 밖")
    if not g("profitable"):
        why_c.append("영업적자")
    if gpm is None or gpm < C.C_GPM_FLOOR:
        why_c.append("매출총이익률 20% 미만")
    if not why_c:
        return "C", []
    return None, ["H4 G 미충족(" + ", ".join(why_g) + ")", "H4 C 미충족(" + ", ".join(why_c) + ")"]


# ---------------- 2층 자동 점수 ----------------

def _band(v, bands):
    """bands: [(하한, 점수), ...] 내림차순. v가 None이면 0."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 0
    for lo, pts in bands:
        if v >= lo:
            return pts
    return 0


def score(passed):
    """1층 통과 종목 목록에 자동 점수를 붙인다 (69점 만점)."""
    if not passed:
        return
    rg_pct = _pct_rank([c.get("rev_growth") for c in passed])
    g_rows = [c for c in passed if c["lane"] == "G"]
    c_rows = [c for c in passed if c["lane"] == "C"]

    def ev_gp_per_growth(c):
        gp, rg, ev = c.get("gross_profit_ttm"), c.get("rev_growth"), c.get("ev")
        if not gp or gp <= 0 or not rg or rg <= 0 or ev is None:
            return None
        return (ev / gp) / (rg * 100)

    g_val_pct = dict(zip([id(c) for c in g_rows], _pct_rank([ev_gp_per_growth(c) for c in g_rows])))
    c_val_pct = dict(zip([id(c) for c in c_rows], _pct_rank([c.get("fcf_yield") for c in c_rows])))

    for c, rp in zip(passed, rg_pct):
        s = {}
        s["S1"] = round((rp or 0) * 8, 1)
        s["S2"] = _band(c.get("accel"), [(0.05, 8), (0.0, 5), (-0.05, 2)])
        s["S3"] = 4 if (c.get("gp_growth") is not None and c.get("rev_growth") is not None
                         and c["gp_growth"] >= c["rev_growth"]) else 0
        s["S5"] = round((c.get("gp_to_assets_ind_pct") or 0) * 8, 1)
        s["S6"] = _band(c.get("opm_change"), [(0.05, 7), (0.02, 4), (0.0, 1)])
        r40 = None
        if c.get("rev_growth") is not None and c.get("fcf_margin") is not None:
            r40 = (c["rev_growth"] + c["fcf_margin"]) * 100
        c["rule_of_40"] = r40
        s["S7"] = _band(r40, [(60, 5), (40, 3)])
        d = c.get("dilution")
        s["S8"] = 0 if d is None else (7 if d < 0 else 5 if d <= 0.02 else 1 if d <= 0.05 else 0)
        slack = None
        if c.get("gp_growth") is not None and c.get("op_asset_growth") is not None:
            slack = c["gp_growth"] - c["op_asset_growth"]
        s["S9"] = _band(slack, [(0.10, 5), (0.0, 2)])
        nd, eb = c.get("net_debt"), c.get("ebitda_ttm")
        if nd is not None and nd <= 0:
            s["S10"] = 3
        elif nd is not None and eb and eb > 0 and nd / eb <= 1:
            s["S10"] = 2
        else:
            s["S10"] = 0
        vp = g_val_pct.get(id(c)) if c["lane"] == "G" else c_val_pct.get(id(c))
        if vp is None or (isinstance(vp, float) and math.isnan(vp)):
            s["S12"] = 0
        else:
            s["S12"] = round(((1 - vp) if c["lane"] == "G" else vp) * 5, 1)
        cap = c.get("mkt_cap") or 0
        s["S13"] = 6 if cap < 2e9 else 4 if cap < 5e9 else 2 if cap < 10e9 else 1
        si = c.get("short_pct_float")
        s["S18s"] = 0 if si is None else (3 if si < 0.05 else 1 if si < 0.10 else 0)
        c["scores"] = s
        c["auto_score"] = round(sum(s.values()), 1)
        c["auto_score_pct"] = c["auto_score"] / C.AUTO_SCORE_MAX
        c["priority"] = c["auto_score_pct"] >= C.PRIORITY_SCORE_PCT


def run(companies):
    """전체 파이프라인. companies: dict 목록 (metrics, meta, 시장 데이터 병합본)."""
    for c in companies:
        c["universe_fail"] = universe_reasons(c)
    universe = [c for c in companies if not c["universe_fail"]]

    # 유니버스 내 상대 지표
    _industry_groups(universe)
    med, p70, _ = _group_stats(universe, "gpm")
    _, _, gpa_pct = _group_stats(universe, "gp_to_assets")
    for c, p in zip(universe, gpa_pct):
        c["ind_gpm_median"] = med.get(c["ind_group"])
        c["ind_gpm_p70"] = p70.get(c["ind_group"])
        c["gp_to_assets_ind_pct"] = None if p is None or (isinstance(p, float) and math.isnan(p)) else p
        cap = c.get("mkt_cap")
        c["fcf_yield"] = (c["fcf_ttm"] / cap) if (c.get("fcf_ttm") is not None and cap) else None
        c["ev"] = (cap + (c.get("net_debt") or 0)) if cap else None
    fy_pct = _pct_rank([c.get("fcf_yield") for c in universe])
    for c, p in zip(universe, fy_pct):
        c["fcf_yield_pct"] = None if p is None or (isinstance(p, float) and math.isnan(p)) else p

    for c in universe:
        ok, lane, reasons = hard_filter(c)
        c["pass"], c["lane"], c["fail"] = ok, lane, reasons
    for c in companies:
        if c["universe_fail"]:
            c["pass"], c["lane"], c["fail"] = False, None, c["universe_fail"]
    return universe


def entry_tags(c):
    """3단계 진입 속도 참고 정보 (점수 아님)."""
    tags = []
    p, hi, lo, ma = c.get("price"), c.get("hi52"), c.get("lo52"), c.get("ma200")
    if p and hi and lo and hi > lo:
        c["range_pos"] = (p - lo) / (hi - lo)
        if c["range_pos"] < 0.5:
            tags.append("12개월 범위 하단")
    if p and ma:
        c["ma200_gap"] = p / ma - 1
        if c["ma200_gap"] > 0.5:
            tags.append("200일선 +50% 이상")
    return tags
