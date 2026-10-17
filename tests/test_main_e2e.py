"""SEC·Yahoo를 가짜 응답으로 바꿔 main() 전체 흐름(이틀 연속 실행, 전일 비교)을 검증한다."""
import datetime as dt
import importlib

import pytest

from tests.test_pipeline import REV, make_facts


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEC_USER_AGENT", "test test@example.com")
    monkeypatch.setenv("SKIP_MAIL", "1")
    import screener.store as store
    importlib.reload(store)
    import screener.main as main
    importlib.reload(main)
    return main, store, tmp_path


def _install_fakes(monkeypatch, main, universe):
    class FakeClient:
        request_count = 0

        def __init__(self, *a, **k):
            pass

        def tickers(self):
            return [{"cik": cik, "name": t, "ticker": t, "exchange": "Nasdaq"} for cik, (t, _) in universe.items()]

        def companyfacts(self, cik):
            return universe[cik][1]

        def submissions(self, cik):
            return {"sic": "7372", "sicDescription": "Software", "name": universe[cik][0],
                    "addresses": {"business": {"stateOrCountry": "CA"}}}

        def daily_index(self, day):
            return []

    monkeypatch.setattr(main, "SecClient", FakeClient)
    px = {"price": 20.0, "price_date": "2026-10-16", "adv20": 20e6, "hi52": 30, "lo52": 10, "ma200": 18}
    monkeypatch.setattr(main.prices, "fetch_prices", lambda tickers: {t: dict(px) for t in tickers})
    monkeypatch.setattr(main.prices, "fetch_short_interest", lambda tickers: {t: 0.03 for t in tickers})


def test_two_day_run(env, monkeypatch):
    main, store, tmp = env
    flat = [100e6 * (1.01 ** i) for i in range(10)]
    universe = {1: ("GOOD", make_facts(REV, gpm=0.6))}
    for i in range(10):
        universe[100 + i] = (f"P{i}", make_facts(flat, gpm=0.4))
    _install_fakes(monkeypatch, main, universe)

    class D1(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 16, 18, 0, tzinfo=tz)

    monkeypatch.setattr(main.dt, "datetime", D1)
    assert main.main() == 0
    res1 = (tmp / "results" / "2026-10-16.csv").read_text(encoding="utf-8-sig")
    assert "GOOD" in res1
    html1 = (tmp / "out" / "latest.html").read_text(encoding="utf-8")
    assert "첫 실행" in html1

    # 둘째 날: GOOD가 대규모 증자로 희석 → 이탈, 신규 종목 NEW 진입
    universe[1] = ("GOOD", make_facts(REV, gpm=0.6, shares_q=[100e6] * 9 + [130e6]))
    universe[2] = ("NEW", make_facts(REV, gpm=0.7))
    (tmp / "cache" / "companies" / "1.json.gz").unlink()   # 재수집 유도

    class D2(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 19, 18, 0, tzinfo=tz)

    monkeypatch.setattr(main.dt, "datetime", D2)
    assert main.main() == 0
    html2 = (tmp / "out" / "latest.html").read_text(encoding="utf-8")
    assert "신규 진입 (1)" in html2 and "NEW" in html2
    assert "이탈 (1)" in html2 and "H1 희석" in html2
    assert (tmp / "snapshots" / "2026-10-19.csv.gz").exists()
