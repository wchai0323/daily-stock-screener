"""한국 일일 실행: python -m screener.kr_main

환경변수
  DART_API_KEY       (필수) DART OpenAPI 인증키
  SMTP_*, MAIL_TO    메일 (미국과 같은 시크릿)
  MAX_COMPANIES      시험 실행용 기업 수 제한
  SKIP_MAIL=1        메일 발송 생략
"""
import datetime as dt
import gzip
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

# 한국 결과는 data/kr 아래에 따로 쌓는다 (store가 import될 때 경로를 정하므로 먼저 설정)
os.environ.setdefault("SCREENER_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "kr"))

from . import dart, facts, markets, metrics, prices, report, screen, store  # noqa: E402
from . import kr_config as K  # noqa: E402
from . import kr_rules as R  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


CACHE = Path(store.ROOT) / "cache"
COMP = CACHE / "companies"


def load_json(path, default):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def load_corp(cc):
    return load_json(COMP / f"{cc}.json.gz", {"reports": {}})


def save_corp(cc, obj):
    save_json(COMP / f"{cc}.json.gz", obj)


# ---------------- 거래소 지정 위험 종목 ----------------

def update_risk_flags(client, state, today):
    """DART 거래소공시(시장조치·안내, I003) 제목으로 관리종목·투자주의환기종목·상장적격성 심사 상태를 추적."""
    flags = {k: set(v) for k, v in (state.get("risk_flags") or {}).items()}
    last = state.get("risk_checked")
    start = dt.date.fromisoformat(last) - dt.timedelta(days=3) if last else today - dt.timedelta(days=365)
    rows = []
    s = start
    while s <= today:                       # 기업 미지정 검색은 3개월 이내만 가능
        e = min(s + dt.timedelta(days=85), today)
        rows.extend(client.filings(s, e, "I", "I003"))
        s = e + dt.timedelta(days=1)
    for r in sorted(rows, key=lambda r: (r.get("rcept_dt") or "", r.get("rcept_no") or "")):
        cc, name = r.get("corp_code"), (r.get("report_nm") or "").replace(" ", "")
        cur = flags.setdefault(cc, set())
        for key in K.RISK_DESIGNATIONS:
            if key not in name:
                continue
            if any(w in name for w in ("우려", "예고", "가능성", "예정")):
                continue                     # 지정 전 경고성 공시는 상태를 바꾸지 않음
            if any(w in name for w in ("해제", "미해당", "유지결정", "상장유지")):
                cur.discard(key)
            elif "지정" in name or "대상" in name or "실질심사" in name:
                cur.add(key)
    state["risk_flags"] = {k: sorted(v) for k, v in flags.items() if v}
    state["risk_checked"] = today.isoformat()
    return state["risk_flags"]


# ---------------- 재무 수집 ----------------

def refresh_info(client, cc, obj, today):
    info = obj.get("info")
    if info and (today - dt.date.fromisoformat(info["fetched"])).days <= K.COMPANY_INFO_MAX_AGE:
        return
    js = client.company(cc) or {}
    obj["info"] = {
        "fetched": today.isoformat(),
        "name": js.get("stock_name") or js.get("corp_name"),
        "market": {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX"}.get(js.get("corp_cls"), "기타"),
        "ksic": js.get("induty_code"),
        "address": js.get("adres"),
        "fiscal_month": int(js["acc_mt"]) if str(js.get("acc_mt") or "").isdigit() else None,
    }


def fetch_report(client, cc, obj, year, code, today):
    """보고서 하나를 받는다. 연결재무 우선, 없으면 별도재무. 결과는 obj['reports'][key]에 저장."""
    key = dart.report_key(year, code)
    order = [obj["fs_div"]] if obj.get("fs_div") else ["CFS", "OFS"]
    rows, used = None, None
    for fs in order:
        rows = client.financials(cc, year, code, fs)
        if rows:
            used = fs
            break
    if not rows and obj.get("fs_div"):        # 기억해 둔 구분에 없으면 반대쪽도 시도
        other = "OFS" if obj["fs_div"] == "CFS" else "CFS"
        rows = client.financials(cc, year, code, other)
        used = other if rows else None
    if not rows:
        obj["reports"][key] = {"status": "missing", "checked": today.isoformat()}
        return False
    if any((r.get("currency") or "KRW") != "KRW" for r in rows[:5]):
        obj["reports"][key] = {"status": "unsupported", "checked": today.isoformat()}
        return False
    obj["fs_div"] = used
    parsed = dart.parse_statement(rows)
    rcept = rows[0].get("rcept_no") or ""
    obj["reports"][key] = {"status": "ok", "checked": today.isoformat(), "rcept_no": rcept, "fs": used,
                           "rows": dart.to_compact_rows(year, code, parsed, rcept)}
    return True


def needs_fetch(rep, year, code, today, filed_recently):
    if filed_recently:
        return True
    if rep is None:
        return today >= dart.due_date(year, code) + dt.timedelta(days=3) or \
            dt.date.fromisoformat(dart.period_end(year, code)) < today - dt.timedelta(days=120)
    if rep.get("status") == "missing":
        tries = rep.get("tries", 1)
        return tries < 3 and (today - dt.date.fromisoformat(rep["checked"])).days >= K.MISSING_RETRY_DAYS
    return False


def build_compact(obj, schedule):
    concepts, shares_out = {}, []
    for year, code in schedule:
        rep = obj["reports"].get(dart.report_key(year, code)) or {}
        if rep.get("status") != "ok":
            continue
        for tag, rows in rep["rows"].items():
            concepts.setdefault(tag, []).extend(rows)
        if rep.get("shares"):
            shares_out.append([dart.period_end(year, code), rep["shares"], None, rep.get("rcept_no")])
    return {"name": (obj.get("info") or {}).get("name"), "concepts": concepts,
            "shares_out": shares_out[-1:], "version": facts.COMPACT_VERSION}


def main():
    today = dt.datetime.now(ZoneInfo("Asia/Seoul")).date()
    log(f"실행일(한국) {today}")
    screen.use(markets.korea())
    client = dart.DartClient(budget=K.DART_DAILY_BUDGET)
    state = load_json(CACHE / "state.json.gz", {})
    schedule = dart.recent_reports(today, K.REPORT_QUARTERS)
    log(f"대상 보고서: {schedule[0]} ~ {schedule[-1]}")
    budget_hit = None
    fetched_reports = 0

    # 1) 상장사 목록 (주 1회 갱신)
    corps = state.get("corps")
    if not corps or (today - dt.date.fromisoformat(state.get("corps_date", "2000-01-01"))).days >= 7:
        corps = client.corp_codes()
        state["corps"], state["corps_date"] = corps, today.isoformat()
    limit = int(os.environ.get("MAX_COMPANIES", "0") or 0)
    if limit:
        corps = corps[:limit]
    log(f"상장사 {len(corps):,}개")

    # 2) 주가: 코스피(.KS)·코스닥(.KQ) 접미사를 모르면 둘 다 시도, 맞는 쪽을 기억
    suffix = state.get("suffix") or {}
    tickers = []
    for c in corps:
        s = suffix.get(c["stock_code"])
        tickers += [c["stock_code"] + s] if s else [c["stock_code"] + ".KS", c["stock_code"] + ".KQ"]
    px_raw = prices.fetch_prices(tickers)
    px = {}
    for t, v in px_raw.items():
        code, sfx = t.split(".")
        suffix[code] = "." + sfx
        px[code] = v
    state["suffix"] = suffix
    log(f"주가 수신 {len(px):,}개")

    # 3) 유동성·주가 사전 필터를 통과한 기업만 DART 재무를 받는다 (호출 수 절약)
    cands = [c for c in corps if c["stock_code"] in px
             and px[c["stock_code"]]["adv20"] >= K.ADV20_MIN and px[c["stock_code"]]["price"] >= K.PRICE_MIN]
    cands.sort(key=lambda c: -px[c["stock_code"]]["adv20"])
    log(f"유동성 통과 {len(cands):,}개")

    try:
        # 4) 최근 정기보고서 제출 기업 (실적 시즌에 새 분기를 바로 반영)
        last = state.get("filings_checked")
        bgn = dt.date.fromisoformat(last) - dt.timedelta(days=2) if last else today - dt.timedelta(days=30)
        recent = {}
        for f in client.filings(bgn, today, "A"):
            parsed = dart.parse_report_name(f.get("report_nm") or "")
            if parsed:
                recent.setdefault(f["corp_code"], set()).add(dart.report_key(parsed[0], parsed[1]))
        state["filings_checked"] = today.isoformat()
        log(f"최근 정기보고서 제출 {len(recent):,}개 기업")

        # 5) 거래소 지정 종목
        risk = update_risk_flags(client, state, today)
        log(f"거래소 지정 종목 {len(risk):,}개")

        # 6) 기업 정보 → 재무 → 주식 수. 최근 제출 기업을 먼저 처리
        cands.sort(key=lambda c: (c["corp_code"] not in recent, -px[c["stock_code"]]["adv20"]))
        for i, c in enumerate(cands, 1):
            cc = c["corp_code"]
            obj = load_corp(cc)
            refresh_info(client, cc, obj, today)
            info = obj["info"]
            # 어차피 제외될 기업은 재무를 받지 않는다
            pre = dict(info, name=info.get("name") or c["corp_name"])
            pre["is_holding"] = R.is_holding(info.get("ksic"), pre["name"])
            pre["is_china"] = R.is_china(info.get("address"), pre["name"])
            if R.universe_extra(pre):
                save_corp(cc, obj)
                continue
            for year, code in schedule:
                key = dart.report_key(year, code)
                rep = obj["reports"].get(key)
                if needs_fetch(rep, year, code, today, key in recent.get(cc, set())):
                    prev_tries = (rep or {}).get("tries", 0)
                    ok = fetch_report(client, cc, obj, year, code, today)
                    fetched_reports += 1
                    if not ok:
                        obj["reports"][key]["tries"] = prev_tries + 1
            # 시총·희석용 주식 수: 받은 보고서 중 최근 것과 그 1년 전 것
            oks = [(y, cd) for y, cd in schedule if (obj["reports"].get(dart.report_key(y, cd)) or {}).get("status") == "ok"]
            for y, cd in (oks[-1:] + oks[-5:-4]) if oks else []:
                rep = obj["reports"][dart.report_key(y, cd)]
                if rep.get("shares") is None and not rep.get("shares_checked"):
                    rep["shares"] = client.shares(cc, y, cd)
                    rep["shares_checked"] = today.isoformat()
                    if rep["shares"]:
                        q, _ = dart.REPORTS[cd]
                        rep["rows"].setdefault(dart.TAG["shares"], []).append(
                            [f"{y}-{dart.Q_START[q]}", dart.period_end(y, cd), rep["shares"], None, rep.get("rcept_no")])
            save_corp(cc, obj)
            if i % 100 == 0:
                log(f"  {i}/{len(cands)} (DART 호출 {client.count:,}건)")
                save_json(CACHE / "state.json.gz", state)
        log(f"보고서 수집 {fetched_reports:,}건")
    except dart.BudgetExceeded as e:
        budget_hit = str(e)
        log(f"DART 예산 소진: {e} — 남은 기업은 다음 실행에서 이어서 받습니다")

    # 7) 지표 계산
    companies = []
    for c in cands:
        obj = load_corp(c["corp_code"])
        info = obj.get("info")
        if not info:
            continue
        comp = build_compact(obj, schedule)
        series = facts.build_series(comp)
        m = metrics.compute(series) or {"quarters": len(series["end"])}
        name = info.get("name") or c["corp_name"]
        row = {"cik": c["corp_code"], "ticker": c["stock_code"] + suffix.get(c["stock_code"], ""),
               "code": c["stock_code"], "name": name, "market": info.get("market"),
               "ksic": info.get("ksic"), "fiscal_month": info.get("fiscal_month"),
               "is_holding": R.is_holding(info.get("ksic"), name), "is_china": R.is_china(info.get("address"), name),
               "risk_flags": (state.get("risk_flags") or {}).get(c["corp_code"], []), "fs_div": obj.get("fs_div"),
               "shares_out": facts.shares_outstanding(comp), "latest_filed": facts.latest_filed(comp)}
        row.update(m)
        row.update(px[c["stock_code"]])
        shares = row.get("shares_out")
        row["mkt_cap"] = row["price"] * shares if shares else None
        mezz_rows = comp["concepts"].get(dart.TAG["mezz"]) or []
        mezz = max(mezz_rows, key=lambda r: r[1])[2] if mezz_rows else None
        row["mezz_ratio"] = (mezz / row["mkt_cap"]) if (mezz is not None and row["mkt_cap"]) else None
        row["sector"] = R.sector_of(row)
        row["industry"] = f"KSIC {row['ksic']}" if row.get("ksic") else None
        companies.append(row)
    log(f"지표 계산 {len(companies):,}개")

    # 8) 필터·점수
    universe = screen.run(companies)
    passed = [c for c in universe if c["pass"]]
    log(f"유니버스 {len(universe):,} → 1층 통과 {len(passed)}")
    qi, yahoo_diag = prices.fetch_quote_info([c["ticker"] for c in passed])
    for c in passed:
        y = qi.get(c["ticker"]) or prices.parse_info({})
        c["pe_fwd"], c["peg"] = y.get("pe_fwd"), y.get("peg")
        cap, rev, ni = c.get("mkt_cap"), c.get("revenue_ttm"), c.get("net_income_ttm")
        c["psr"] = cap / rev if (cap and rev and rev > 0) else None
        c["pe_ttm"] = cap / ni if (cap and ni and ni > 0) else None
        c["entry_tags"] = screen.entry_tags(c)
    screen.score(passed)

    # 9) 저장·비교·메일
    res_df = report.to_frame(sorted(passed, key=lambda c: -c["auto_score"]), report.RESULT_COLS)
    res_df.insert(0, "date", today.isoformat())
    prev_day, prev_df = store.previous_results(today)
    res_path = store.save_results(today, res_df)
    snap = report.to_frame(universe, report.SNAPSHOT_COLS)
    snap["criteria_version"] = K.CRITERIA_VERSION
    store.save_snapshot(today, snap)
    new, exits = report.diff(passed, prev_df, companies, store.load_snapshot(prev_day))

    # 보유 종목은 6자리 종목코드로 적는다 → 오늘 쓰는 티커(005930.KS 등)로 맞춘다
    holdings = {h + suffix.get(h, "") if h.isdigit() else h for h in store.load_holdings("holdings_kr.txt")}
    stats = {"universe": len(universe), "refreshed": fetched_reports}
    html_body = report.render(today, prev_day, passed, new, exits, holdings, companies, stats)
    if budget_hit:
        html_body = html_body.replace(
            "</h1>", "</h1><p style='color:#C00000;font-size:12px'>DART 초기 수집이 아직 진행 중입니다. "
                     "일부 기업은 재무가 부족해 오늘 결과에서 빠졌고, 다음 실행에서 이어서 받습니다.</p>", 1)
    out = Path(store.ROOT) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.html").write_text(html_body, encoding="utf-8")
    subject = f"{screen.M.SUBJECT} {today} · 통과 {len(passed)}"
    if new is not None:
        subject += f" · 신규 {len(new)} · 이탈 {len(exits)}"
    if os.environ.get("SKIP_MAIL") == "1":
        mail_status = "skipped (skip_mail)"
    else:
        from . import mailer
        try:
            mailer.send(subject, html_body, [res_path])
            mail_status = "sent"
        except Exception as e:  # noqa: BLE001
            mail_status = f"failed: {type(e).__name__}: {e}"
            print(f"::warning title=메일 발송 실패::{mail_status}", flush=True)

    save_json(CACHE / "state.json.gz", state)
    complete = sum(1 for c in companies if (c.get("quarters") or 0) >= K.MIN_QUARTERS)
    store.save_run_summary({
        "run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "screen_date": today.isoformat(), "listed": len(corps), "with_prices": len(px),
        "liquid_candidates": len(cands), "with_8q_financials": complete, "universe": len(universe),
        "passed": len(passed), "lanes": {"G": sum(c["lane"] == "G" for c in passed),
                                         "C": sum(c["lane"] == "C" for c in passed)},
        "new": None if new is None else len(new), "exits": None if exits is None else len(exits),
        "prices": dict(prices.LAST_DIAG), "sample_tickers": tickers[:6],
        "dart_calls": client.count, "dart_budget_hit": budget_hit, "mail": mail_status, "yahoo": yahoo_diag,
    })
    log(f"완료 (DART 호출 {client.count:,}건, 메일 {mail_status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
