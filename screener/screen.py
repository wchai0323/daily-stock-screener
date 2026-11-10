"""0단계 유니버스 → 1층 하드 필터 → 2층 자동 점수.

입력: 기업별 dict (metrics + meta + 시장 데이터). 출력: 같은 dict에 판정·사유·점수를 붙인 목록.
"""
import bisect
import math

import pandas as pd

from . import markets

# 현재 시장 프로필. 미국이 기본이고, 한국 실행은 use(markets.korea())로 바꾼다
M = markets.US


def use(market):
    global M
    M = market


def _excluded_sic(sic):   # 미국 main의 사전 필터용 (하위 호환)
    return markets._us_excluded_sic(sic)


def _pct_rank(values):
    """None은 제외하고 백분위(0~1)를 매긴다. 반환은 원래 순서의 리스트."""
    s = pd.Series(values, dtype="float64")
    return s.rank(pct=True).where(s.notna()).tolist()


def t_enabled():
    return bool(getattr(M, "T_LANE_ENABLED", False))


def _universe_cap_max():
    """T 차선이 켜져 있으면 유니버스 시총 상한을 T 기준(500억 달러)으로 넓힌다. G/C는 hard_filter에서 H3으로 거른다."""
    return M.T_MKT_CAP_MAX if t_enabled() else M.MKT_CAP_MAX


def universe_reasons(c):
    """0단계 탈락 사유 목록 (빈 목록이면 통과)."""
    r = list(M.universe_extra(c))
    if c.get("quarters", 0) < M.MIN_QUARTERS:
        r.append("분기 데이터 8개 미만")
    rev = c.get("revenue_ttm")
    if rev is None or rev < M.REVENUE_TTM_MIN:
        r.append(f"H3 매출 {M.fmt_money(M.REVENUE_TTM_MIN)} 미만")
    cap = c.get("mkt_cap")
    if cap is None:
        r.append("시가총액 확인 불가")
    elif cap < M.MKT_CAP_MIN:
        r.append(f"시가총액 {M.fmt_money(M.MKT_CAP_MIN)} 미만")
    elif cap > _universe_cap_max():
        r.append(f"H3 시가총액 {M.fmt_money(_universe_cap_max())} 초과")
    if (c.get("adv20") or 0) < M.ADV20_MIN:
        r.append("거래대금 부족")
    if (c.get("price") or 0) < M.PRICE_MIN:
        r.append(f"주가 {M.PRICE_LABEL} 미만")
    return r


def _industry_groups(rows, ref=None):
    """세부 업종 → 큰 업종 → 전체 순으로 표본이 8개 이상인 비교 그룹을 정한다 (미국 SIC, 한국 KSIC).
    ref를 주면 표본 수는 ref 기준으로 센다 (T 차선 대형주를 G/C 유니버스 업종 그룹에 붙일 때)."""
    keys = [M.industry_keys(c) for c in rows]
    cnt_f, cnt_c = {}, {}
    for fine, coarse in ([M.industry_keys(c) for c in ref] if ref is not None else keys):
        if fine:
            cnt_f[fine] = cnt_f.get(fine, 0) + 1
        if coarse:
            cnt_c[coarse] = cnt_c.get(coarse, 0) + 1
    for c, (fine, coarse) in zip(rows, keys):
        if fine and cnt_f.get(fine, 0) >= M.INDUSTRY_MIN_PEERS:
            c["ind_group"] = fine
        elif coarse and cnt_c.get(coarse, 0) >= M.INDUSTRY_MIN_PEERS:
            c["ind_group"] = coarse
        else:
            c["ind_group"] = "ALL"


def _pct_against(rows, ref, key):
    """rows 각각의 key 값이 ref 중 같은 업종 그룹 안에서 몇 백분위인지 (그룹 표본이 없으면 None)."""
    by = {}
    for r in ref:
        v = r.get(key)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            by.setdefault(r["ind_group"], []).append(v)
    for vals in by.values():
        vals.sort()
    out = []
    for c in rows:
        v, vals = c.get(key), by.get(c.get("ind_group"))
        if v is None or (isinstance(v, float) and math.isnan(v)) or not vals:
            out.append(None)
        else:
            out.append(bisect.bisect_right(vals, v) / len(vals))
    return out


def _group_stats(rows, key):
    """그룹별 지표 분포: 40백분위(흑자 G 기준), 70백분위(적자 G 기준), 그룹 내 백분위."""
    df = pd.DataFrame({"g": [c["ind_group"] for c in rows], "v": [c.get(key) for c in rows]})
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    med = df.groupby("g")["v"].quantile(M.G_GPM_PROFITABLE_PCTL).to_dict()
    p70 = df.groupby("g")["v"].quantile(M.G_GPM_UNPROFITABLE_PCTL).to_dict()
    pct = df.groupby("g")["v"].rank(pct=True).tolist()
    return med, p70, pct


def hard_filter(c):
    """1층. 반환: (통과 여부, 차선 'G'/'C'/None, 탈락 사유 목록)"""
    reasons = []
    g = lambda k: c.get(k)

    # H1 희석
    if g("dilution") is None:
        reasons.append("H1 희석 데이터 없음")
    elif g("dilution") > M.H1_DILUTION_MAX:
        reasons.append(f"H1 희석 {g('dilution'):+.1%}")

    # H2 재무 생존력
    if g("profitable"):
        nde = g("net_debt_ebitda")
        if g("net_debt") is not None and g("net_debt") <= 0:
            pass  # 순현금
        elif nde is None or nde > M.H2_NET_DEBT_EBITDA_MAX:
            reasons.append("H2 순부채/EBITDA 2배 초과" if nde is not None else "H2 부채 판정 불가")
    else:
        runway = g("runway_months")
        if runway is None:
            reasons.append("H2 런웨이 판정 불가")
        elif runway < M.H2_RUNWAY_MONTHS_MIN:
            reasons.append(f"H2 런웨이 {runway:.0f}개월")

    # H5 블랙홀: 적자면 영업이익률 1년 +5%p 이상 개선
    if not g("profitable"):
        ch = g("opm_change")
        if ch is None or ch < M.H5_OPM_IMPROVEMENT_MIN:
            reasons.append("H5 적자 + 마진 개선 부족")

    # H6 투자 효율: 영업자산 증가율 <= 매출총이익 증가율
    # 매출총이익이 없는 기업(매출원가 미공시)은 매출 증가율로 대신 비교
    oa, gpg = g("op_asset_growth"), g("gp_growth")
    label = "매출총이익"
    if gpg is None and g("gpm") is None:
        gpg, label = g("rev_growth"), "매출"
    if oa is None or gpg is None:
        reasons.append("H6 판정 불가")
    elif oa > gpg:
        reasons.append(f"H6 자산 증가({oa:+.0%}) > {label} 증가({gpg:+.0%})")

    # 시장 고유 위험 필터 (한국: 메자닌 잔액, 관리종목 등)
    reasons.extend(M.extra_hard(c))

    # H3 시총: G/C는 200억 달러까지 (유니버스는 T 차선 때문에 500억 달러까지 넓혀져 있음)
    if (g("mkt_cap") or 0) > M.MKT_CAP_MAX:
        reasons.append(f"H3 시가총액 {M.fmt_money(M.MKT_CAP_MAX)} 초과")

    # H4 성장 차선
    lane, lane_reasons = _lane(c)
    if lane is None:
        reasons.extend(lane_reasons)
    if not reasons:
        return True, lane, []

    # T 차선: G/C 탈락 종목만 별도 기준으로 다시 판정
    if t_enabled():
        t_why = t_lane_reasons(c)
        if not t_why:
            return True, "T", []
        if t_industry(c):
            reasons.append("T 미충족(" + ", ".join(t_why) + ")")
    return False, lane, reasons


def t_industry(c):
    sic = c.get("sic")
    if sic is None:
        return False
    if any(lo <= sic <= hi for lo, hi in M.T_SIC_EXCLUDED_RANGES):
        return False
    return any(lo <= sic <= hi for lo, hi in M.T_SIC_RANGES)


def t_lane_reasons(c):
    """T 차선(하드웨어 성장) 미충족 사유. 빈 목록이면 통과."""
    g = lambda k: c.get(k)
    why = []
    if not t_industry(c):
        return ["업종(기계·전자·계측·전문건설) 아님"]
    rg, rev = g("rev_growth"), g("revenue_ttm")
    if rg is not None and rev is not None and rev / (1 + rg) < M.PRIOR_REVENUE_MIN:
        return ["기저 매출 너무 작음"]
    if rg is None or rg < M.T_REV_GROWTH_MIN:
        why.append(f"성장 {M.T_REV_GROWTH_MIN:.0%} 미만")
    q = g("q_yoy")
    if q is None or q < M.T_QTR_YOY_MIN:
        why.append(f"최근 분기 YoY {M.T_QTR_YOY_MIN:.0%} 미만")
    ch = g("opm_change")
    if ch is None or ch < M.T_OPM_CHANGE_MIN:
        why.append("영업이익률 2%p 초과 악화")
    gpm = g("gpm")
    if gpm is not None and gpm < M.T_GPM_MIN:
        why.append(f"매출총이익률 {M.T_GPM_MIN:.0%} 미만")
    d = g("dilution")
    if d is None:
        why.append("희석 데이터 없음")
    elif d > M.T_DILUTION_MAX:
        why.append(f"희석 {d:+.0%}")
    if g("profitable"):
        nd, nde = g("net_debt"), g("net_debt_ebitda")
        if not (nd is not None and nd <= 0) and (nde is None or nde > M.T_NET_DEBT_EBITDA_MAX):
            why.append(f"순부채/EBITDA {M.T_NET_DEBT_EBITDA_MAX:.0f}배 초과" if nde is not None else "부채 판정 불가")
    else:
        rw = g("runway_months")
        if rw is None or rw < M.T_RUNWAY_MONTHS_MIN:
            why.append("런웨이 24개월 미만" if rw is not None else "런웨이 판정 불가")
    v, basis = valuation(c, "G")
    if v is not None and v > M.VAL_G_EXTREME:
        why.append(f"성장 대비 극단 고평가({v:.2f})")
    return why


def market_cap_shares(cover_shares, diluted_shares):
    """시가총액용 주식 수. 주식 종류가 여럿인 회사는 SEC 표지에 일부 종류만 나올 수 있어
    (예: HEICO), 모든 종류를 포함하는 손익계산서 희석 주식 수와 비교해 큰 값을 쓴다."""
    vals = [v for v in (cover_shares, diluted_shares) if v]
    return max(vals) if vals else None


def is_cyclical(c):
    return M.is_cyclical(c)


def valuation(c, lane="G"):
    """성장 대비 밸류에이션 배수. 반환 (값, 근거). 낮을수록 싸다. 계산 불가면 (None, None).

    G: EV/매출총이익 ÷ 매출성장률(%). 매출총이익 미공시면 EV/영업이익 ÷ 성장률(%)
    C: EV/FCF ÷ 매출성장률(%)
    성장률은 VAL_GROWTH_CAP(100%)에서 자른다. EV가 음수(순현금 > 시총)면 음수가 나오며 '매우 쌈'으로 본다.
    """
    ev, rg, rev = c.get("ev"), c.get("rev_growth"), c.get("revenue_ttm")
    if ev is None or rg is None or rg <= 0:
        return None, None
    g = min(rg, M.VAL_GROWTH_CAP) * 100
    if lane == "C":
        fcf = c.get("fcf_ttm")
        if fcf is None or fcf <= 0:
            return None, None
        return ev / fcf / g, "EV/FCF"
    gp = c.get("gross_profit_ttm")
    if gp is None and c.get("gpm") is not None and rev:
        gp = c["gpm"] * rev
    if gp is not None and gp > 0:
        return ev / gp / g, "EV/매출총이익"
    opm = c.get("opm")
    if opm is not None and opm > 0 and rev:
        return ev / (opm * rev) / g, "EV/영업이익"
    return None, None


def valuation_tag(v, lane):
    """'성장 대비 저평가' / '성장 대비 고평가' / None."""
    if v is None:
        return None
    cheap, rich = (M.VAL_C_CHEAP, M.VAL_C_RICH) if lane == "C" else (M.VAL_G_CHEAP, M.VAL_G_RICH)
    if v < cheap:
        return "성장 대비 저평가"
    if v >= rich:
        return "성장 대비 고평가"
    return None


def apply_valuation(c):
    """차선이 정해진 통과 종목에 밸류 값·근거·태그를 붙인다."""
    lane = c.get("lane") or "G"
    v, basis = valuation(c, lane)
    c["val_ratio"], c["val_basis"], c["val_tag"] = v, basis, valuation_tag(v, lane)
    return c


def _lane(c):
    g = lambda k: c.get(k)
    rg, gpm = g("rev_growth"), g("gpm")
    why_g, why_c = [], []

    # 기저 효과: 1년 전 매출이 너무 작으면 두 차선 모두 성장률 조건 불충족
    rev = g("revenue_ttm")
    if rg is not None and rev is not None and rev / (1 + rg) < M.PRIOR_REVENUE_MIN:
        return None, [f"H4 기저 매출 너무 작음 (1년 전 TTM {M.fmt_money(rev / (1 + rg))}, 성장률 왜곡)"]

    # G 차선
    if rg is None or rg < M.G_REV_GROWTH_MIN:
        why_g.append("성장 25% 미만")
    if g("accel") is None or g("accel") <= M.G_ACCEL_FLOOR:
        why_g.append("성장 급감속")
    if gpm is None:
        # 매출원가 미공시: 매출총이익률 조건 대신 영업이익률이 악화되지 않았는지 확인
        if g("opm_change") is None or g("opm_change") < M.G_NO_GPM_OPM_CHANGE_MIN:
            why_g.append("매출총이익률 미공시 + 영업이익률 악화")
    else:
        if g("profitable"):
            floor = max(M.G_GPM_FLOOR_PROFITABLE, g("ind_gpm_median") or 0)
        else:
            floor = max(M.G_GPM_FLOOR_UNPROFITABLE, g("ind_gpm_p70") or 0)
        if gpm < floor:
            why_g.append(f"매출총이익률 {gpm:.0%} < 기준 {floor:.0%}")
        if g("gpm_prev") is not None and g("gpm_prev") - gpm > M.G_GPM_DECLINE_MAX:
            why_g.append("매출총이익률 2%p 초과 하락")
    # 성장 대비 극단 고평가 (2026-11-10). 지표 계산 불가면 판정하지 않는다
    v, basis = valuation(c, "G")
    if v is not None and v > M.VAL_G_EXTREME:
        why_g.append(f"성장 대비 극단 고평가({basis}÷성장률 {v:.2f})")
    if not why_g:
        return "G", []

    # C 차선
    if rg is None or rg < M.C_REV_GROWTH_MIN:
        why_c.append("성장 10% 미만")
    if g("fcf_yield_pct") is None or g("fcf_yield_pct") < M.C_FCF_YIELD_PCTL:
        why_c.append("FCF 수익률 상위 30% 밖")
    if not g("profitable"):
        why_c.append("영업적자")
    if gpm is not None and gpm < M.C_GPM_FLOOR:   # 미공시는 영업흑자 조건으로 갈음
        why_c.append("매출총이익률 20% 미만")
    if (g("mkt_cap") or 0) > M.C_MKT_CAP_MAX:
        why_c.append(f"시총 {M.fmt_money(M.C_MKT_CAP_MAX)} 초과")
    if is_cyclical(c):
        why_c.append("경기 민감 원자재·해운 업종")
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
    """1층 통과 종목 목록에 자동 점수를 붙인다 (미국 74점, 한국 71점 만점)."""
    if not passed:
        return
    rg_pct = _pct_rank([c.get("rev_growth") for c in passed])
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
        # S12 성장 조정 밸류 (10점, 절대 구간). 2026-11-10 전에는 통과 종목 내 상대 순위 5점
        apply_valuation(c)
        v = c["val_ratio"]
        if v is None:
            s["S12"] = M.S12_MISSING
        else:
            bands = M.S12_C_BANDS if c["lane"] == "C" else M.S12_G_BANDS
            s["S12"] = next((pts for hi, pts in bands if v < hi), 0)
        cap = c.get("mkt_cap") or 0
        s["S13"] = next((pts for lim, pts in M.CAP_BANDS if cap < lim), 1)
        if M.USE_SHORT:
            si = c.get("short_pct_float")
            s["S18s"] = 0 if si is None else (3 if si < 0.05 else 1 if si < 0.10 else 0)
        c["scores"] = s
        c["auto_score"] = round(sum(s.values()), 1)
        c["auto_score_pct"] = c["auto_score"] / M.AUTO_SCORE_MAX
        c["priority"] = c["auto_score_pct"] >= M.PRIORITY_SCORE_PCT


def run(companies):
    """전체 파이프라인. companies: dict 목록 (metrics, meta, 시장 데이터 병합본)."""
    for c in companies:
        c["universe_fail"] = universe_reasons(c)
    universe = [c for c in companies if not c["universe_fail"]]
    # 업종 통계·백분위는 G/C 유니버스(시총 200억 달러 이하)로만 계산한다. T 차선 대형주는 그 분포에 붙인다
    core = [c for c in universe if (c.get("mkt_cap") or 0) <= M.MKT_CAP_MAX]
    ext = [c for c in universe if (c.get("mkt_cap") or 0) > M.MKT_CAP_MAX]

    _industry_groups(core)
    if ext:
        _industry_groups(ext, ref=core)
    med, p70, _ = _group_stats(core, "gpm")
    _, _, gpa_pct = _group_stats(core, "gp_to_assets")
    gpa_ext = _pct_against(ext, core, "gp_to_assets")
    for c, p in zip(core + ext, list(gpa_pct) + gpa_ext):
        c["ind_gpm_median"] = med.get(c["ind_group"])
        c["ind_gpm_p70"] = p70.get(c["ind_group"])
        c["gp_to_assets_ind_pct"] = None if p is None or (isinstance(p, float) and math.isnan(p)) else p
        cap = c.get("mkt_cap")
        c["fcf_yield"] = (c["fcf_ttm"] / cap) if (c.get("fcf_ttm") is not None and cap) else None
        c["ev"] = (cap + (c.get("net_debt") or 0)) if cap else None
    fy_pct = _pct_rank([c.get("fcf_yield") for c in core])
    for c, p in zip(core, fy_pct):
        c["fcf_yield_pct"] = None if p is None or (isinstance(p, float) and math.isnan(p)) else p
    core_fy = sorted(c["fcf_yield"] for c in core if c.get("fcf_yield") is not None)
    for c in ext:
        fy = c.get("fcf_yield")
        c["fcf_yield_pct"] = None if fy is None or not core_fy else bisect.bisect_right(core_fy, fy) / len(core_fy)

    for c in universe:
        ok, lane, reasons = hard_filter(c)
        c["pass"], c["lane"], c["fail"] = ok, lane, reasons
    for c in companies:
        if c["universe_fail"]:
            c["pass"], c["lane"], c["fail"] = False, None, c["universe_fail"]
    return universe


def entry_tags(c):
    """3단계 진입 속도 참고 정보와 주의 표시 (점수 아님)."""
    tags = list(M.extra_tags(c))
    apply_valuation(c)   # 밸류 태그는 메일의 밸류 칸과 CSV val_tag에 표시
    if is_cyclical(c):
        tags.append("경기 민감 업종")
    if c.get("lane") == "T":
        if (c.get("dilution") or 0) >= M.T_DILUTION_WARN:
            tags.append("희석 주의")
        nde = c.get("net_debt_ebitda")
        if (c.get("net_debt") or 0) > 0 and nde is not None and nde > M.T_DEBT_WARN:
            tags.append("부채 주의")
    if c.get("gpm") is None:
        tags.append("매출총이익률 미공시")
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
