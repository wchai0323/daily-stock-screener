"""한국 파이프라인: DART 응답 → 분기 시계열 → 지표 → 필터 → 메일. 네트워크 없이 가짜 응답으로 검증."""
import datetime as dt
import importlib

import pytest

from screener import dart, facts, kr_rules, markets, metrics, screen

# 2024Q3 ~ 2026Q3 (9개 분기)
SCHEDULE = dart.recent_reports(dt.date(2026, 10, 20), 9)
QREV = [100e8, 110e8, 125e8, 140e8, 150e8, 170e8, 190e8, 215e8, 240e8]   # 분기 매출 (원)


def fake_statement(year, code, rev_q, gpm=0.45, opm=0.15, mezz=0.0):
    """year·code 보고서의 fnlttSinglAcntAll 응답 행. rev_q: {(year, q): 분기 매출}."""
    q = dart.REPORTS[code][0]
    qs = [rev_q[(year, i)] for i in range(1, q + 1)]
    cur, ytd = qs[-1], sum(qs)
    annual = code == "11011"
    is_q = ytd if annual else cur

    def isrow(aid, nm, ratio):
        return {"sj_div": "IS", "account_id": aid, "account_nm": nm, "currency": "KRW",
                "thstrm_amount": f"{is_q * ratio:,.0f}",
                "thstrm_add_amount": "" if annual or q == 1 else f"{ytd * ratio:,.0f}", "rcept_no": "20261114000123"}

    rows = [
        isrow("ifrs-full_Revenue", "매출액", 1.0),
        isrow("ifrs-full_CostOfSales", "매출원가", 1 - gpm),
        isrow("ifrs-full_GrossProfit", "매출총이익", gpm),
        isrow("dart_OperatingIncomeLoss", "영업이익(손실)", opm),
        isrow("ifrs-full_ProfitLoss", "당기순이익(손실)", opm * 0.8),
        isrow("ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업의 소유주", opm * 0.75),
        {"sj_div": "CF", "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities", "account_nm": "영업활동현금흐름",
         "thstrm_amount": f"{ytd * 0.18:,.0f}"},
        {"sj_div": "CF", "account_id": "-표준계정코드 미사용-", "account_nm": "유형자산의 취득",
         "thstrm_amount": f"-{ytd * 0.04:,.0f}"},
        {"sj_div": "CF", "account_id": "ifrs-full_AdjustmentsForDepreciationExpense", "account_nm": "감가상각비",
         "thstrm_amount": f"{ytd * 0.02:,.0f}"},
        {"sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계", "thstrm_amount": f"{2000e8 + cur:,.0f}"},
        {"sj_div": "BS", "account_id": "ifrs-full_CashAndCashEquivalents", "account_nm": "현금및현금성자산",
         "thstrm_amount": f"{300e8:,.0f}"},
        {"sj_div": "BS", "account_id": "ifrs-full_ShorttermBorrowings", "account_nm": "단기차입금",
         "thstrm_amount": f"{50e8:,.0f}"},
        {"sj_div": "BS", "account_id": "-표준계정코드 미사용-", "account_nm": "전환사채", "thstrm_amount": f"{mezz:,.0f}"},
        {"sj_div": "BS", "account_id": "-표준계정코드 미사용-", "account_nm": "전환사채할인발행차금",
         "thstrm_amount": f"{-mezz * 0.1:,.0f}"},
    ]
    return rows


def rev_map(qrev):
    out = {}
    for (year, code), v in zip(SCHEDULE, qrev):
        out[(year, dart.REPORTS[code][0])] = v
    # 2024년 1·2분기 (2024 3분기 누적 계산용)
    out.setdefault((2024, 1), qrev[0] * 0.9)
    out.setdefault((2024, 2), qrev[0] * 0.95)
    return out


def compact_for(qrev, **kw):
    rm = rev_map(qrev)
    concepts = {}
    for year, code in SCHEDULE:
        parsed = dart.parse_statement(fake_statement(year, code, rm, **kw))
        for tag, rows in dart.to_compact_rows(year, code, parsed, "20261114000123", shares=10_000_000).items():
            concepts.setdefault(tag, []).extend(rows)
    return {"name": "테스트", "concepts": concepts, "shares_out": [["2026-09-30", 10_000_000, None, "x"]],
            "version": facts.COMPACT_VERSION}


def test_schedule_and_report_names():
    assert SCHEDULE[0] == (2024, "11014") and SCHEDULE[-1] == (2026, "11014")
    assert dart.parse_report_name("[기재정정]분기보고서 (2026.09)") == (2026, "11014", True)
    assert dart.parse_report_name("사업보고서 (2025.12)") == (2025, "11011", False)
    assert dart.parse_report_name("주요사항보고서(유상증자결정)") is None
    assert dart.due_date(2025, "11011") == dt.date(2026, 3, 31)


def test_dart_statement_to_metrics():
    comp = compact_for(QREV, mezz=200e8)
    series = facts.build_series(comp)
    assert len(series["end"]) == 9
    # 4분기(사업보고서 연간 − 1~3분기)도 정확히 복원
    assert series["end"][1] == "2024-12-31"
    assert abs(series["revenue"][1] - QREV[1]) < 1
    m = metrics.compute(series)
    ttm, prev = sum(QREV[-4:]), sum(QREV[-8:-4])
    assert abs(m["revenue_ttm"] - ttm) < 1
    assert abs(m["rev_growth"] - (ttm / prev - 1)) < 1e-9
    assert abs(m["gpm"] - 0.45) < 1e-9
    assert abs(m["net_income_ttm"] - ttm * 0.15 * 0.75) < 1      # 지배주주 순이익 우선
    assert abs(m["fcf_ttm"] - ttm * 0.14) < 1e3                   # 영업현금 18% − 설비투자 4% (음수 표기도 처리)
    assert m["debt"] == 50e8 + 200e8                              # 단기차입금 + 전환사채 (할인차금 제외)
    mezz_rows = comp["concepts"]["MezzanineDebt"]
    assert max(mezz_rows, key=lambda r: r[1])[2] == 200e8


def test_kr_rules():
    assert kr_rules.is_holding("64992", "어떤회사") and kr_rules.is_holding("70113", "LS홀딩스")
    assert kr_rules.is_china("중국 산둥성 ...", "") and kr_rules.is_china("", "차이나그레이트")
    base = {"market": "KOSDAQ", "ksic": "26110", "name": "반도체회사", "fiscal_month": 12}
    assert kr_rules.universe_extra(base) == []
    assert "금융업" in kr_rules.universe_extra(dict(base, ksic="64121"))
    assert kr_rules.universe_extra(dict(base, ksic="64992", is_holding=True)) == []
    assert "스팩" in kr_rules.universe_extra(dict(base, name="하나30호스팩"))
    assert "코스피·코스닥 외 시장" in kr_rules.universe_extra(dict(base, market="KONEX"))
    assert kr_rules.is_cyclical({"ksic": "20111"}) and kr_rules.is_cyclical({"ksic": "50112"})
    assert not kr_rules.is_cyclical({"ksic": "26110"})
    assert kr_rules.sector_of({"ksic": "26110"}) == "반도체·전자부품·통신장비"
    assert kr_rules.krw(1.23e12) == "1.2조원" and kr_rules.krw(3456e8) == "3,456억원"
    assert any("CB·BW" in r for r in kr_rules.extra_hard({"mezz_ratio": 0.15}))
    assert any("관리종목" in r for r in kr_rules.extra_hard({"risk_flags": ["관리종목"]}))


def test_china_controlling_holder():
    from screener import dart
    rows = [
        {"nm": "동심반도체주식유한공사 (Dosilicon Co., Ltd.)", "relate": "본인", "stock_knd": "보통주",
         "trmend_posesn_stock_qota_rt": "30.20"},
        {"nm": "안승한", "relate": "임원", "stock_knd": "보통주", "trmend_posesn_stock_qota_rt": "0.29"},
        {"nm": "계", "relate": "-", "stock_knd": "보통주", "trmend_posesn_stock_qota_rt": "30.49"},
    ]
    hs = dart.parse_major_holders(rows)
    assert [h["name"][:5] for h in hs] == ["동심반도체", "안승한"] and hs[0]["pct"] == 30.2
    ch = kr_rules.china_holder(hs)
    assert ch and ch["pct"] == 30.2
    base = {"market": "KOSDAQ", "ksic": "26110", "name": "피델릭스", "fiscal_month": 12}
    assert any("중국계 지배주주" in r for r in kr_rules.universe_extra(dict(base, china_holder=ch)))
    assert any("수동 목록" in r for r in kr_rules.universe_extra(dict(base, china_listed=True)))
    # 국내 그룹의 중국 현지법인이 특수관계인으로 소량 보유한 경우는 제외하지 않는다
    korean = [{"name": "삼성전자", "relate": "본인", "pct": 20.0},
              {"name": "삼성(중국)투자유한공사", "relate": "계열회사", "pct": 0.3}]
    assert kr_rules.china_holder(korean) is None
    assert kr_rules.china_holder([{"name": "Hong Kong Holdings Ltd", "relate": "계열회사", "pct": 12.0}])
    assert kr_rules.largest_holder_tag(hs).startswith("최대주주 동심반도체")


@pytest.fixture
def kr_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DART_API_KEY", "test")
    monkeypatch.setenv("SKIP_MAIL", "1")
    import screener.store as store
    importlib.reload(store)
    import screener.kr_main as kr_main
    importlib.reload(kr_main)
    yield kr_main, tmp_path
    screen.use(markets.US)


def test_kr_main_end_to_end(kr_env, monkeypatch):
    kr_main, tmp = kr_env
    flat = [100e8 * (1.01 ** i) for i in range(9)]
    corps = {
        "00000001": ("111110", "성장전자", "26110", "경기도 성남시", QREV, 0.0),
        "00000002": ("222220", "메자닌많은", "26110", "서울", QREV, 900e8),     # CB 잔액이 시총의 20%
        "00000003": ("333330", "중국기업", "26110", "중국 산둥성", QREV, 0.0),
        "00000004": ("444440", "관리기업", "26110", "서울", QREV, 0.0),
        "00000005": ("666660", "중국계지배", "26110", "경기도 성남시", QREV, 0.0),   # 한국 본사, 중국계 최대주주
    }
    for i in range(10):
        corps[f"0000010{i}"] = (f"55555{i}", f"비교군{i}", "26110", "서울", flat, 0.0)

    class FakeDart:
        count = 0

        def __init__(self, *a, **k):
            pass

        def corp_codes(self):
            return [{"corp_code": cc, "corp_name": v[1], "stock_code": v[0]} for cc, v in corps.items()]

        def company(self, cc):
            v = corps[cc]
            return {"corp_name": v[1], "stock_name": v[1], "corp_cls": "K", "induty_code": v[2], "adres": v[3],
                    "acc_mt": "12"}

        def financials(self, cc, year, code, fs):
            if fs != "CFS":
                return None
            v = corps[cc]
            return fake_statement(year, code, rev_map(v[4]), mezz=v[5])

        def shares(self, cc, year, code):
            return 10_000_000

        def major_holders(self, cc, year, code):
            if cc == "00000005":
                return [{"name": "동심반도체주식유한공사", "relate": "본인", "pct": 30.2}]
            return [{"name": "창업자", "relate": "본인", "pct": 25.0}]

        def filings(self, bgn, end, ty, detail=None):
            if ty == "I":
                return [{"corp_code": "00000004", "report_nm": "관리종목지정(감사의견 거절)", "rcept_dt": "20260401",
                         "rcept_no": "1"}]
            return [{"corp_code": "00000001", "report_nm": "분기보고서 (2026.09)"}]

    monkeypatch.setattr(kr_main.dart, "DartClient", FakeDart)
    px = {"price": 45000.0, "price_date": "2026-10-20", "adv20": 50e8, "hi52": 60000, "lo52": 30000, "ma200": 42000}
    monkeypatch.setattr(kr_main.kr_prices, "fetch", lambda codes, today=None: {c: dict(px) for c in codes})
    monkeypatch.setattr(kr_main.prices, "fetch_quote_info",
                        lambda tickers: ({t: kr_main.prices.parse_info({"forwardPE": 15.0}) for t in tickers}, {}))

    class D(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 20, 16, 30, tzinfo=tz)

    monkeypatch.setattr(kr_main.dt, "datetime", D)
    assert kr_main.main() == 0
    res = (tmp / "results" / "2026-10-20.csv").read_text(encoding="utf-8-sig")
    html = (tmp / "out" / "latest.html").read_text(encoding="utf-8")
    assert "111110.KQ" in res                              # 성장전자 통과 (시총 4,500억 원)
    assert "222220" not in res and "333330" not in res and "444440" not in res and "666660" not in res
    assert "최대주주 창업자 25.0%" in html
    assert "한국 주식 스크리너" in html and "4,500억원" in html and "반도체·전자부품·통신장비" in html
    snap = (tmp / "snapshots" / "2026-10-20.csv.gz")
    import pandas as pd
    s = pd.read_csv(snap)
    assert s["criteria_version"].iloc[0].startswith("KR-")
    import json
    summary = json.loads((tmp / "last_run.json").read_text(encoding="utf-8"))
    assert summary["passed"] >= 1 and summary["dart_budget_hit"] is None


def test_parse_naver_sise():
    from screener import kr_prices, prices
    text = """[['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],
["20261016", 44000, 45500, 43800, 45000, 120000, 12.3],
["20261019", 45000, 46000, 44500, 45500, 100000, 12.4]
]"""
    df = kr_prices.parse_naver(text)
    assert list(df["Close"]) == [45000, 45500] and str(df.index[-1].date()) == "2026-10-19"
    assert prices.summarize(df) is None          # 20일 미만이면 요약하지 않음
