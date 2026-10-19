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
    info = {"shortPercentOfFloat": 0.03, "trailingPE": 31.4, "forwardPE": 22.0, "trailingPegRatio": 1.15,
            "sector": "Technology", "industry": "Software - Application"}
    monkeypatch.setattr(main.prices, "fetch_quote_info",
                        lambda tickers: ({t: main.prices.parse_info(info) for t in tickers}, {"errors": {}}))


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
    # 밸류에이션·분류가 메일과 CSV에 들어간다
    assert "PER 31.4 / 22.0" in html1 and "PEG 1.15" in html1 and "PSR" in html1
    assert "Technology" in html1 and "Software - Application" in html1
    assert "pe_fwd" in res1 and "sector" in res1

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


def test_mail_secret_missing_is_recorded(env, monkeypatch, capsys):
    """메일 시크릿이 없으면 실행은 성공하되, 실행 요약과 Actions 경고에 실패가 남아야 한다."""
    import json

    main, store, tmp = env
    monkeypatch.delenv("SKIP_MAIL")
    for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    _install_fakes(monkeypatch, main, {1: ("GOOD", make_facts(REV, gpm=0.6))})
    assert main.main() == 0
    summary = json.loads((tmp / "last_run.json").read_text(encoding="utf-8"))
    assert summary["mail"].startswith("failed: MailConfigError")
    assert "SMTP_PASSWORD" in summary["mail"]
    assert summary["max_companies"] is None and summary["targets"] == 1
    assert "::warning title=메일 발송 실패::" in capsys.readouterr().out
