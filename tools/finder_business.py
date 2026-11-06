"""파인더 테마 태깅용: DART 사업보고서의 '사업의 내용'을 회사별로 추출한다.

출력: data/finder/kr_business/{종목코드}.json  (이미 있으면 건너뜀 → 여러 번 나눠 돌려도 이어서 진행)
      data/finder/kr_business_index.json       (수집 요약)

회사별 JSON
  code, name, corp_code, cls(Y=코스피, K=코스닥), rcept_no, report_nm, rcept_dt
  overview  : 1. 사업의 개요 (앞부분)
  products  : 2. 주요 제품 및 서비스 (매출 비중 표 포함)
  sales     : 매출 및 수주상황 (앞부분)
  fallback  : 위 소제목을 못 찾으면 '사업의 내용' 앞부분

환경변수
  DART_API_KEY   (필수)
  MAX_COMPANIES  시험용 기업 수 제한
  DEADLINE_MIN   이번 실행 수집 시간 한도(분), 기본 300
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from screener.dart import DartClient, BudgetExceeded, DartError  # noqa: E402

OUT = "data/finder/kr_business"
INDEX = "data/finder/kr_business_index.json"
LIM = {"overview": 5000, "products": 4000, "sales": 2500, "fallback": 7000}


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------- 대상 목록: 최근 1년 사업보고서 ----------------

def annual_filings(client, today):
    """최근 13개월 사업보고서(A001). 회사당 가장 최근 접수 1건 (정정본이 나중이면 정정본)."""
    latest = {}
    end = today
    start_all = today - dt.timedelta(days=760)   # 최신 문서가 없을 때 쓸 직전 연도분까지
    while end > start_all:
        bgn = max(start_all, end - dt.timedelta(days=88))
        for r in client.filings(bgn, end, "A", "A001"):
            if r.get("corp_cls") not in ("Y", "K"):
                continue
            stock = (r.get("stock_code") or "").strip()
            if not re.fullmatch(r"\d{6}", stock):
                continue
            if "사업보고서" not in (r.get("report_nm") or ""):
                continue
            latest.setdefault(stock, []).append(r)
        end = bgn - dt.timedelta(days=1)
    for v in latest.values():                      # 최신순. 최신 문서가 없으면(014) 다음 것을 쓴다
        v.sort(key=lambda x: x["rcept_no"], reverse=True)
    cut = (today - dt.timedelta(days=400)).strftime("%Y%m%d")
    return {k: v for k, v in latest.items() if v[0]["rcept_dt"] >= cut}   # 대상은 최근 13개월 제출사만


# ---------------- 문서 파싱 ----------------

def decode(b):
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "ignore")


def _table(m):
    """표 안의 줄바꿈 태그를 없애고 한 행을 'a | b | c' 한 줄로 만든다."""
    t = re.sub(r"\s+", " ", m.group(0))                       # 원문 XML 줄바꿈 제거
    t = re.sub(r"(?is)<(P|BR|SPAN)[^>]*>|</(P|SPAN)>", " ", t)
    t = re.sub(r"(?is)<TR[^>]*>", "\n", t)
    t = re.sub(r"(?is)<(TD|TH|TE|TU)[^>]*>", " | ", t)
    return t


def to_text(xml):
    s = re.sub(r"(?is)<TABLE[^>]*>.*?</TABLE>", _table, xml)
    s = re.sub(r"(?is)<(TABLE-GROUP|TABLE)[^>]*>", "\n[표]\n", s)
    s = re.sub(r"(?is)</(TABLE-GROUP|TABLE)>", "\n[/표]\n", s)
    s = re.sub(r"(?is)<TR[^>]*>", "\n", s)
    s = re.sub(r"(?is)<(TD|TH|TE|TU)[^>]*>", " | ", s)
    s = re.sub(r"(?is)<(P|BR|TITLE|SECTION-\d)[^>]*>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"( \| )+\n", "\n", s)
    s = re.sub(r"\n\s*\|\s*", "\n", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


SEC2 = re.compile(r"(?is)<SECTION-2[^>]*>\s*<TITLE[^>]*>(.*?)</TITLE>")


def business_section(xml):
    """'II. 사업의 내용' SECTION-1 블록을 찾는다."""
    m = re.search(r"(?is)<SECTION-1[^>]*>\s*<TITLE[^>]*>\s*I{2}\.\s*사업의\s*내용\s*</TITLE>", xml)
    if not m:
        m = re.search(r"(?is)<TITLE[^>]*>[^<]{0,10}사업의\s*내용\s*</TITLE>", xml)
    if not m:
        return None
    rest = xml[m.end():]
    n = re.search(r"(?is)<SECTION-1[^>]*>\s*<TITLE[^>]*>\s*I{3}\.", rest)
    return rest[: n.start()] if n else rest[:400000]


def split_sub(sec):
    """SECTION-2 단위로 (제목, 본문XML) 목록."""
    hits = list(SEC2.finditer(sec))
    out = []
    for i, h in enumerate(hits):
        body = sec[h.end(): hits[i + 1].start() if i + 1 < len(hits) else len(sec)]
        out.append((re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h.group(1))).strip(), body))
    return out


def pick(subs, *keys):
    for title, body in subs:
        t = title.replace(" ", "")
        if any(k in t for k in keys):
            return to_text(body)
    return None


def extract(zbytes):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        names = sorted(z.namelist(), key=lambda n: len(n))     # 본문이 보통 가장 짧은 이름
        docs = [decode(z.read(n)) for n in names if n.lower().endswith(".xml")]
    for xml in docs:
        sec = business_section(xml)
        if not sec:
            continue
        subs = split_sub(sec)
        out = {
            "overview": pick(subs, "사업의개요"),
            "products": pick(subs, "주요제품", "주요상품", "제품및서비스", "영업의현황", "영업의개황"),
            "sales": pick(subs, "매출및수주", "매출에관한", "수주상황"),
        }
        if not out["overview"] and not out["products"]:
            out["fallback"] = to_text(sec)
        return {k: v[: LIM[k]] for k, v in out.items() if v}
    return None


# ---------------- 실행 ----------------

def main():
    today = dt.date.today()
    deadline = time.time() + 60 * float(os.environ.get("DEADLINE_MIN") or 300)
    client = DartClient(budget=19000, min_interval=0.12, deadline=deadline)
    os.makedirs(OUT, exist_ok=True)

    targets = annual_filings(client, today)
    log(f"최근 사업보고서 제출 상장사 {len(targets)}곳")
    force = os.environ.get("FORCE") == "1"                     # 파서 수정 후 전체 다시 받기
    todo = [rs for s, rs in sorted(targets.items()) if force or not os.path.exists(f"{OUT}/{s}.json")]
    mx = os.environ.get("MAX_COMPANIES")
    if mx:
        todo = todo[: int(mx)]
    log(f"이번 실행 대상 {len(todo)}곳")

    stats = {"ok": 0, "nosection": 0, "error": 0}
    errors = []

    def one(rs):
        for r in rs:
            res = client._get("document.xml", rcept_no=r["rcept_no"])
            if res.content.startswith(b"PK"):
                break
        else:
            raise DartError(res.text[:200])
        body = extract(res.content)
        rec = {"code": r["stock_code"], "name": r["corp_name"], "corp_code": r["corp_code"],
               "cls": r["corp_cls"], "rcept_no": r["rcept_no"], "report_nm": r["report_nm"],
               "rcept_dt": r["rcept_dt"]}
        if body:
            rec.update(body)
        with open(f"{OUT}/{r['stock_code']}.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        return bool(body)

    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(one, r): r for r in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            r = futs[fu][0]
            try:
                stats["ok" if fu.result() else "nosection"] += 1
            except BudgetExceeded as e:
                log(f"중단: {e}")
                ex.shutdown(cancel_futures=True)
                break
            except Exception as e:  # noqa: BLE001
                stats["error"] += 1
                errors.append([r["stock_code"], r["corp_name"], str(e)[:150]])
            if i % 100 == 0:
                log(f"{i}/{len(todo)} {stats}")

    done = sorted(f[:-5] for f in os.listdir(OUT) if f.endswith(".json"))
    idx = {"asof": today.isoformat(), "targets": len(targets), "collected": len(done),
           "remaining": len(set(targets) - set(done)), "this_run": stats, "errors": errors[:200],
           "dart_calls": client.count}
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    log(json.dumps({k: v for k, v in idx.items() if k != "errors"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
