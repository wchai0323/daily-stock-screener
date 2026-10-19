"""일일 실행 진입점: python -m screener.main

환경변수
  SEC_USER_AGENT   (필수) SEC 요청용 연락처. 예: 'Daily Stock Screener you@example.com'
  SMTP_HOST/PORT/USER/PASSWORD, MAIL_TO, MAIL_FROM  메일 발송 (없으면 발송 생략)
  MAX_COMPANIES    시험 실행용 기업 수 제한 (예: 300)
  SKIP_MAIL=1      메일 발송 생략
"""
import datetime as dt
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as C
from . import facts, metrics, prices, report, screen, store
from .sec import SecClient


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pick_tickers(rows):
    """CIK당 대표 티커 하나 (클래스 구분 기호가 없는 짧은 티커 우선)."""
    by_cik = {}
    for r in rows:
        if r.get("exchange") not in C.EXCHANGES or not r.get("ticker"):
            continue
        rank = lambda t: (("-" in t) or ("." in t), len(t))
        cur = by_cik.get(r["cik"])
        if cur is None or rank(r["ticker"]) < rank(cur["ticker"]):
            by_cik[r["cik"]] = r
    return by_cik


def recent_filers(client, state, today):
    last = state.get("last_index_date")
    start = (dt.date.fromisoformat(last) if last else today) - dt.timedelta(days=C.INDEX_LOOKBACK_DAYS)
    ciks, d = set(), start
    while d <= today:
        if d.weekday() < 5:
            for f in client.daily_index(d):
                if f["form"] in C.FILING_FORMS:
                    ciks.add(f["cik"])
        d += dt.timedelta(days=1)
    return ciks


def refresh_company(client, cik, existing, today):
    fj = client.companyfacts(cik)
    comp = facts.compact(fj) if fj else {"name": None, "concepts": {}, "shares_out": []}
    obj = existing or {}
    obj["compact"] = comp
    obj["fetched"] = today.isoformat()
    # 기업 메타(SIC·주소)는 매출 기준을 넘는 기업만, 90일마다 갱신 (요청 수 절약)
    need_meta = not obj.get("meta") or (
        today - dt.date.fromisoformat(obj.get("meta_fetched", "2000-01-01"))).days > 90
    if need_meta:
        series = facts.build_series(comp)
        rev = series["revenue"][-4:]
        if len(rev) == 4 and sum(rev) >= C.REVENUE_TTM_MIN:
            sub = client.submissions(cik)
            if sub:
                addr = (sub.get("addresses") or {}).get("business") or {}
                obj["meta"] = {
                    "sic": int(sub["sic"]) if str(sub.get("sic") or "").isdigit() else None,
                    "sic_desc": sub.get("sicDescription"),
                    "country": addr.get("stateOrCountry"),
                    "state_inc": sub.get("stateOfIncorporation"),
                    "name": sub.get("name"),
                }
                obj["meta_fetched"] = today.isoformat()
    return obj


def main():
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    log(f"실행일(미국 동부) {today}")
    client = SecClient()
    state = store.load_state()
    fetched = state.get("fetched", {})

    # 1) 종목 목록
    tick = pick_tickers(client.tickers())
    targets = sorted(tick)
    limit = int(os.environ.get("MAX_COMPANIES", "0") or 0)
    if limit:
        targets = targets[:limit]
    log(f"대상 기업 {len(targets):,}개")

    # 2) 재무 갱신 대상: 캐시 없음 + 최근 공시 + 오래된 캐시
    cached = store.cached_ciks()
    recent = recent_filers(client, state, today)
    missing = [c for c in targets if c not in cached]
    filed = [c for c in targets if c in cached and c in recent]
    stale = sorted((c for c in targets if c in cached and c not in recent),
                   key=lambda c: fetched.get(str(c), "2000-01-01"))
    stale = [c for c in stale if (today - dt.date.fromisoformat(fetched.get(str(c), "2000-01-01"))).days
             > C.FACTS_MAX_AGE_DAYS][:C.STALE_REFRESH_PER_RUN]
    to_refresh = missing + filed + stale
    log(f"재무 갱신: 신규 {len(missing)} · 최근 공시 {len(filed)} · 오래된 캐시 {len(stale)}")
    for i, cik in enumerate(to_refresh, 1):
        try:
            obj = refresh_company(client, cik, store.load_company(cik), today)
            store.save_company(cik, obj)
            fetched[str(cik)] = today.isoformat()
        except Exception as e:  # noqa: BLE001 - 한 기업 오류로 전체가 멈추지 않게
            log(f"  CIK {cik} 갱신 실패: {e}")
        if i % 250 == 0:
            log(f"  {i}/{len(to_refresh)} (SEC 요청 {client.request_count:,}건)")
            state["fetched"] = fetched
            store.save_state(state)

    # 3) 지표 계산
    companies = []
    for cik in targets:
        obj = store.load_company(cik)
        if not obj:
            continue
        comp = obj["compact"]
        series = facts.build_series(comp)
        m = metrics.compute(series) or {"quarters": len(series["end"])}
        meta = obj.get("meta") or {}
        c = {"cik": cik, "ticker": tick[cik]["ticker"], "exchange": tick[cik]["exchange"],
             "name": meta.get("name") or comp.get("name") or tick[cik].get("name"),
             "sic": meta.get("sic"), "country": meta.get("country"),
             "shares_out": facts.shares_outstanding(comp), "latest_filed": facts.latest_filed(comp)}
        c.update(m)
        companies.append(c)
    log(f"지표 계산 완료 {len(companies):,}개")

    # 4) 주가: 재무 기준(매출·분기·업종·국가)을 먼저 통과한 종목만 다운로드
    pre = [c for c in companies if c.get("quarters", 0) >= C.MIN_QUARTERS
           and (c.get("revenue_ttm") or 0) >= C.REVENUE_TTM_MIN
           and not screen._excluded_sic(c.get("sic")) and c.get("country") not in C.CHINA_LOCATION_CODES]
    log(f"주가 다운로드 {len(pre):,}개")
    px = prices.fetch_prices([c["ticker"] for c in pre])
    for c in companies:
        p = px.get(c["ticker"])
        if p:
            c.update(p)
            shares = c.get("shares_out") or c.get("diluted_shares_last")
            c["mkt_cap"] = c["price"] * shares if shares else None
    log(f"주가 수신 {len(px):,}개")

    # 5) 0단계·1층
    universe = screen.run(companies)
    passed = [c for c in universe if c["pass"]]
    log(f"유니버스 {len(universe):,} → 1층 통과 {len(passed)}")

    # 6) 2층 자동 점수 + 밸류에이션·분류 (Yahoo 조회는 통과 종목만)
    qi, yahoo_diag = prices.fetch_quote_info([c["ticker"] for c in passed])
    log(f"Yahoo 조회: {yahoo_diag}")
    for c in passed:
        c.update(qi.get(c["ticker"]) or prices.parse_info({}))
        rev = c.get("revenue_ttm")
        c["psr"] = c["mkt_cap"] / rev if (c.get("mkt_cap") and rev and rev > 0) else None
        c["entry_tags"] = screen.entry_tags(c)
    screen.score(passed)

    # 7) 저장·비교
    res_df = report.to_frame(sorted(passed, key=lambda c: -c["auto_score"]), report.RESULT_COLS)
    res_df.insert(0, "date", today.isoformat())
    prev_day, prev_df = store.previous_results(today)
    res_path = store.save_results(today, res_df)
    store.save_snapshot(today, report.to_frame(universe, report.SNAPSHOT_COLS))
    new, exits = report.diff(passed, prev_df, companies)

    # 8) 메일
    holdings = store.load_holdings()
    html_body = report.render(today, prev_day, passed, new, exits, holdings, companies,
                              {"universe": len(universe), "refreshed": len(to_refresh)})
    out = Path(store.ROOT) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.html").write_text(html_body, encoding="utf-8")
    subject = f"[스크리너] {today} · 통과 {len(passed)}"
    if new is not None:
        subject += f" · 신규 {len(new)} · 이탈 {len(exits)}"
    if os.environ.get("SKIP_MAIL") == "1":
        mail_status = "skipped (skip_mail)"
    else:
        from . import mailer
        try:
            mailer.send(subject, html_body, [res_path])
            mail_status = "sent"
        except Exception as e:  # noqa: BLE001 - 결과 저장은 계속하고 실패를 기록·경고
            mail_status = f"failed: {type(e).__name__}: {e}"
            # GitHub Actions 화면에 경고로 표시
            print(f"::warning title=메일 발송 실패::{mail_status}", flush=True)

    state["fetched"] = fetched
    state["last_index_date"] = today.isoformat()
    store.save_state(state)

    # 실행 요약: 로그 없이도 결과를 확인할 수 있게 저장소에 남긴다
    summary = {
        "run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "screen_date": today.isoformat(),
        "max_companies": limit or None,
        "targets": len(targets),
        "refreshed": {"new": len(missing), "recent_filers": len(filed), "stale": len(stale)},
        "with_financials": len(companies),
        "with_prices": len(px),
        "universe": len(universe),
        "passed": len(passed),
        "lanes": {"G": sum(c["lane"] == "G" for c in passed), "C": sum(c["lane"] == "C" for c in passed)},
        "priority": sum(bool(c.get("priority")) for c in passed),
        "new": None if new is None else len(new),
        "exits": None if exits is None else len(exits),
        "mail": mail_status,
        "yahoo": yahoo_diag,
        "sec_requests": client.request_count,
    }
    store.save_run_summary(summary)
    log(f"완료 (SEC 요청 {client.request_count:,}건, 메일 {mail_status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
