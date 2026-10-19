"""합성 XBRL 데이터로 분기화·지표·필터·점수 로직을 검증한다 (네트워크 불필요)."""
import datetime as dt

import pytest

from screener import facts, metrics, screen, report, sec


def _qdates(n, last_end=dt.date(2026, 6, 30)):
    """회계연도 = 달력연도. 최근 분기말부터 거꾸로 n개의 (start, end)."""
    ends = []
    y, m = last_end.year, last_end.month
    for _ in range(n):
        ends.append(dt.date(y, m, 30 if m in (6, 9) else 31))
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    ends.reverse()
    out = []
    for e in ends:
        sm = e.month - 2
        out.append((dt.date(e.year, sm, 1), e))
    return out


def make_facts(rev_q, gpm=0.6, opm_q=None, shares_q=None, cash=500e6, assets_q=None,
               ocf_ratio=0.15, capex_ratio=0.03, debt=0.0):
    """분기 매출 목록으로 10-Q(1~3분기 직접값, 현금흐름은 누적값)·10-K(연간값) 형식의 companyfacts를 만든다."""
    n = len(rev_q)
    periods = _qdates(n)
    opm_q = opm_q or [0.1] * n
    shares_q = shares_q or [100e6] * n
    assets_q = assets_q or [1000e6 + 20e6 * i for i in range(n)]
    tags = {k: [] for k in ["Revenues", "CostOfRevenue", "OperatingIncomeLoss",
                            "NetCashProvidedByUsedInOperatingActivities",
                            "PaymentsToAcquirePropertyPlantAndEquipment", "DepreciationDepletionAndAmortization",
                            "WeightedAverageNumberOfDilutedSharesOutstanding", "Assets",
                            "CashAndCashEquivalentsAtCarryingValue", "LongTermDebt", "NetIncomeLoss"]}

    def add(tag, start, end, val, form):
        rec = {"end": end.isoformat(), "val": val, "form": form, "filed": (end + dt.timedelta(days=35)).isoformat(),
               "accn": f"acc-{end}"}
        if start is not None:
            rec["start"] = start.isoformat()
        tags[tag].append(rec)

    by_year = {}
    for i, (s, e) in enumerate(periods):
        by_year.setdefault(e.year, []).append(i)
    for year, idxs in by_year.items():
        fy_start = dt.date(year, 1, 1)
        ytd = {"ocf": 0, "capex": 0, "da": 0}
        for i in idxs:
            s, e = periods[i]
            q_in_year = (e.month) // 3
            r = rev_q[i]
            ytd["ocf"] += r * ocf_ratio
            ytd["capex"] += r * capex_ratio
            ytd["da"] += r * 0.02
            form = "10-K" if q_in_year == 4 else "10-Q"
            if q_in_year < 4:   # 손익 분기값은 10-Q에만
                add("Revenues", s, e, r, form)
                add("CostOfRevenue", s, e, r * (1 - gpm), form)
                add("OperatingIncomeLoss", s, e, r * opm_q[i], form)
                add("NetIncomeLoss", s, e, r * opm_q[i] * 0.75, form)
                add("WeightedAverageNumberOfDilutedSharesOutstanding", s, e, shares_q[i], form)
            # 현금흐름은 연초부터 누적값
            add("NetCashProvidedByUsedInOperatingActivities", fy_start, e, ytd["ocf"], form)
            add("PaymentsToAcquirePropertyPlantAndEquipment", fy_start, e, ytd["capex"], form)
            add("DepreciationDepletionAndAmortization", fy_start, e, ytd["da"], form)
            add("Assets", None, e, assets_q[i], form)
            add("CashAndCashEquivalentsAtCarryingValue", None, e, cash, form)
            add("LongTermDebt", None, e, debt, form)
            if q_in_year == 4:  # 10-K 연간값
                full = [j for j in idxs]
                if len(full) == 4:
                    add("Revenues", fy_start, e, sum(rev_q[j] for j in full), form)
                    add("CostOfRevenue", fy_start, e, sum(rev_q[j] * (1 - gpm) for j in full), form)
                    add("OperatingIncomeLoss", fy_start, e, sum(rev_q[j] * opm_q[j] for j in full), form)
                    add("NetIncomeLoss", fy_start, e, sum(rev_q[j] * opm_q[j] * 0.75 for j in full), form)
                    add("WeightedAverageNumberOfDilutedSharesOutstanding", fy_start, e, shares_q[i], form)
    units = lambda tag: "shares" if "Shares" in tag else "USD"
    gaap = {t: {"units": {units(t): rows}} for t, rows in tags.items() if rows}
    dei = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": periods[-1][1].isoformat(), "val": shares_q[-1], "filed": "2026-08-05", "accn": "acc-last", "form": "10-Q"}]}}}
    return {"entityName": "TEST CO", "facts": {"us-gaap": gaap, "dei": dei}}


# 10분기: 2024Q1 ~ 2026Q2, 분기 매출이 꾸준히 성장
REV = [100, 108, 117, 126, 136, 147, 160, 175, 192, 212]
REV = [r * 1e6 for r in REV]


def test_quarterize_derives_q4_and_cashflow():
    comp = facts.compact(make_facts(REV))
    s = facts.build_series(comp)
    assert len(s["end"]) == 10
    # 4분기 매출 = 연간 - 1~3분기 합 (2024Q4 = 126)
    assert s["revenue"][3] == pytest.approx(126e6)
    # 현금흐름 분기값 = 누적값 차감 (2025Q3 OCF = 160 * 0.15)
    assert s["ocf"][6] == pytest.approx(160e6 * 0.15)
    assert s["gross_profit"][-1] == pytest.approx(212e6 * 0.6)
    assert facts.shares_outstanding(comp) == 100e6


def test_metrics_values():
    comp = facts.compact(make_facts(REV))
    m = metrics.compute(facts.build_series(comp))
    ttm, prev = sum(REV[-4:]), sum(REV[-8:-4])
    assert m["revenue_ttm"] == pytest.approx(ttm)
    assert m["rev_growth"] == pytest.approx(ttm / prev - 1)
    assert m["gpm"] == pytest.approx(0.6)
    assert m["q_yoy"] == pytest.approx(212 / 147 - 1)
    yoy = [212 / 147 - 1, 192 / 136 - 1, 175 / 126 - 1]
    assert m["accel"] == pytest.approx(yoy[0] - (yoy[1] + yoy[2]) / 2)
    assert m["dilution"] == pytest.approx(0.0)
    assert m["fcf_ttm"] == pytest.approx(ttm * 0.12)
    assert m["profitable"] is True


def test_metrics_needs_8_quarters():
    comp = facts.compact(make_facts(REV[:6]))
    assert metrics.compute(facts.build_series(comp)) is None


def _company(ticker, fj, cap=2e9, sic=7372, **extra):
    comp = facts.compact(fj)
    c = {"ticker": ticker, "cik": hash(ticker) % 10**6, "name": ticker, "exchange": "Nasdaq",
         "sic": sic, "country": "CA", "shares_out": facts.shares_outstanding(comp),
         "price": 20.0, "adv20": 20e6, "mkt_cap": cap, "hi52": 25, "lo52": 10, "ma200": 18}
    c.update(metrics.compute(facts.build_series(comp)))
    c.update(extra)
    return c


def _peers(n=10):
    """같은 업종 비교군: 매출총이익률 40%, 저성장."""
    flat = [100e6 * (1.01 ** i) for i in range(10)]
    return [_company(f"P{i}", make_facts(flat, gpm=0.40), cap=1.5e9) for i in range(n)]


def test_g_lane_pass_and_scoring():
    good = _company("GOOD", make_facts(REV, gpm=0.6))
    rows = [good] + _peers()
    screen.run(rows)
    assert good["pass"], good["fail"]
    assert good["lane"] == "G"
    screen.score([good])
    assert 0 < good["auto_score"] <= 69
    assert good["scores"]["S8"] == 5   # 희석 0%


def test_dilution_and_low_margin_fail():
    diluter = _company("DIL", make_facts(REV, shares_q=[100e6 * (1.03 ** i) for i in range(10)]))
    # 업종 중앙값(40%) 미만 → G 탈락. 현금흐름이 좋으면 C 차선으로는 통과
    low_gpm_cash = _company("LOWC", make_facts(REV, gpm=0.30))
    # 같은 조건에 현금흐름까지 약하면 두 차선 모두 탈락
    low_gpm_weak = _company("LOWW", make_facts(REV, gpm=0.30, ocf_ratio=0.0))
    rows = [diluter, low_gpm_cash, low_gpm_weak] + _peers()
    screen.run(rows)
    assert not diluter["pass"] and any(r.startswith("H1") for r in diluter["fail"])
    assert low_gpm_cash["pass"] and low_gpm_cash["lane"] == "C"
    assert not low_gpm_weak["pass"]
    assert any("매출총이익률" in r for r in low_gpm_weak["fail"])


def test_hardware_peer_group_allows_low_absolute_margin():
    """업종 중앙값이 25%인 하드웨어 업종에서는 매출총이익률 30%도 G 차선을 통과한다."""
    hw_flat = [100e6 * (1.01 ** i) for i in range(10)]
    peers = [_company(f"H{i}", make_facts(hw_flat, gpm=0.25), sic=3572) for i in range(10)]
    hw = _company("HWG", make_facts(REV, gpm=0.30), sic=3572)
    screen.run([hw] + peers)
    assert hw["pass"], hw["fail"]


def test_unprofitable_without_margin_improvement_fails_h5():
    loss = _company("LOSS", make_facts(REV, opm_q=[-0.2] * 10))
    screen.run([loss] + _peers())
    assert any(r.startswith("H5") for r in loss["fail"])


def test_universe_exclusions():
    bank = _company("BANK", make_facts(REV), sic=6022)
    china = _company("CHN", make_facts(REV), country="F4")
    big = _company("BIG", make_facts(REV), cap=50e9)
    screen.run([bank, china, big] + _peers())
    assert "금융·REIT·SPAC 업종" in bank["fail"]
    assert any(r.startswith("H7") for r in china["fail"])
    assert any("200억" in r for r in big["fail"])


def test_diff_and_render():
    good = _company("GOOD", make_facts(REV))
    rows = [good] + _peers()
    screen.run(rows)
    passed = [c for c in rows if c["pass"]]
    for c in passed:
        c["entry_tags"] = screen.entry_tags(c)
    screen.score(passed)
    import pandas as pd
    prev = pd.DataFrame({"ticker": ["OLD", "GOOD"]})
    new, exits = report.diff(passed, prev, rows)
    assert new == [] and exits[0]["ticker"] == "OLD"
    html = report.render(dt.date(2026, 10, 19), "2026-10-16", passed, new, exits, {"OLD"}, rows,
                         {"universe": len(rows), "refreshed": 3})
    assert "GOOD" in html and "이탈 (1)" in html


def test_parse_form_index():
    text = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
10-K/A      SOME CORP                                                     1234567     20261016    edgar/data/1234567/0001234567-26-000010.txt
10-Q        APPLE INC                                                     320193      20261016    edgar/data/320193/0000320193-26-000073.txt
8-K         OTHER CO INC                                                  99999       20261016    edgar/data/99999/0000099999-26-000001.txt
"""
    rows = sec.parse_form_index(text)
    assert [(r["form"], r["cik"]) for r in rows] == [("10-K/A", 1234567), ("10-Q", 320193), ("8-K", 99999)]


def test_no_gross_margin_disclosed_uses_operating_margin():
    """매출원가 미공시 기업: 매출총이익률 조건을 건너뛰고 영업이익률 추세로 G 차선 판정."""
    nogp = _company("NOGP", make_facts(REV, gpm=0.6), gpm=None, gpm_prev=None, gp_growth=None)
    worse = _company("WRS", make_facts(REV, gpm=0.6), gpm=None, gpm_prev=None, gp_growth=None,
                     opm_change=-0.03)
    screen.run([nogp, worse] + _peers())
    assert nogp["pass"] and nogp["lane"] == "G", nogp["fail"]
    # 영업이익률이 악화되면 G 차선은 탈락 (현금흐름이 좋으면 C 차선으로는 통과 가능)
    assert worse["lane"] != "G"


def test_c_lane_excludes_commodities_and_large_caps():
    """C 차선: 원자재 업종(SIC 1311 석유가스 채굴)과 시총 100억 달러 초과는 제외."""
    energy = _company("OIL", make_facts(REV, gpm=0.30), sic=1311)
    flat = [100e6 * (1.01 ** i) for i in range(10)]
    energy_peers = [_company(f"E{i}", make_facts(flat, gpm=0.40), sic=1311) for i in range(10)]
    large = _company("LRG", make_facts(REV, gpm=0.30), cap=12e9)
    ok = _company("OKC", make_facts(REV, gpm=0.30))
    screen.run([energy, large, ok] + _peers() + energy_peers)
    assert ok["pass"] and ok["lane"] == "C", ok["fail"]
    assert not energy["pass"] and any("원자재" in r for r in energy["fail"])
    assert not large["pass"] and any("100억" in r for r in large["fail"])


def test_tiny_base_revenue_growth_rejected():
    """1년 전 매출이 2,500만 달러 미만이면 성장률을 믿지 않는다 (기저 효과)."""
    tiny = [1e6] * 6 + [3e6, 8e6, 15e6, 30e6]   # 1년 전 TTM 400만 → 현재 5,600만 달러
    c = _company("TINY", make_facts(tiny, gpm=0.8))
    screen.run([c] + _peers())
    assert not c["pass"] and any("기저 매출" in r for r in c["fail"]), c["fail"]


def test_cyclical_tag_in_g_lane():
    ship = _company("SHIP", make_facts(REV, gpm=0.6), sic=4412)
    assert "경기 민감 업종" in screen.entry_tags(ship)


def test_parse_info_and_valuation_cell():
    from screener import prices, report
    v = prices.parse_info({"trailingPE": None, "forwardPE": "Infinity", "pegRatio": 2.3, "sector": ""})
    assert v["pe_ttm"] is None and v["pe_fwd"] is None and v["peg"] == 2.3 and v["sector"] is None
    cell = report._valuation({"pe_ttm": None, "pe_fwd": 40.0, "peg": None, "psr": 6.25, "opm": -0.1})
    assert "PER 적자 / 40.0" in cell and "PSR 6.2" in cell


def test_parse_yahoo_raw_responses():
    from screener import prices
    qs = {"quoteSummary": {"result": [{
        "assetProfile": {"sector": "Technology", "industry": "Semiconductors"},
        "summaryDetail": {"trailingPE": {"raw": 31.2, "fmt": "31.20"}, "forwardPE": {"raw": 20.5}},
        "defaultKeyStatistics": {"pegRatio": {"raw": 1.3}, "shortPercentOfFloat": {"raw": 0.04}}}]}}
    v = prices.parse_info(prices.parse_quote_summary(qs))
    assert v == {"short_pct_float": 0.04, "pe_ttm": 31.2, "pe_fwd": 20.5, "peg": 1.3,
                 "sector": "Technology", "industry": "Semiconductors"}
    batch = {"quoteResponse": {"result": [{"symbol": "AAA", "trailingPE": 15.0, "forwardPE": 12.0}]}}
    assert prices.parse_info(prices.parse_quote_batch(batch)["AAA"])["pe_fwd"] == 12.0


def test_net_income_ttm_and_sectors():
    from screener import sectors
    m = metrics.compute(facts.build_series(facts.compact(make_facts(REV))))
    # 최근 4분기 매출 739M × 영업이익률 10% × 0.75
    assert abs(m["net_income_ttm"] - sum(REV[-4:]) * 0.1 * 0.75) < 1
    assert sectors.sector_of(3674) == "반도체·전자부품"
    assert sectors.sector_of(7372) == "소프트웨어·IT서비스"
    assert sectors.sector_of(3812) == "항공·방산"
    assert sectors.industry_of("SEMICONDUCTORS & RELATED DEVICES") == "Semiconductors & Related Devices"
