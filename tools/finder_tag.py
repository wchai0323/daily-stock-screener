"""파인더 테마 태깅: 사업의 내용 → 테마·순도·근거 문장·한 줄 사업 설명.

입력: data/finder/kr_business/{code}.json, config/finder_themes.json
출력: data/finder/kr_tags/{code}.json  (있으면 건너뜀 → 여러 번 나눠 실행해도 이어서)
      data/finder/kr_tags_index.json

AI 제공자 (먼저 있는 것을 씀)
  ANTHROPIC_API_KEY  → Anthropic API (TAG_MODEL, 기본 claude-haiku-5-5)
  GITHUB_TOKEN       → GitHub Models (TAG_MODEL, 기본 openai/gpt-4.1-mini). 무료 한도가 작아 하루 수백 곳 수준
환경변수: MAX_COMPANIES, DEADLINE_MIN(기본 300)
"""
import datetime as dt
import json
import os
import re
import time
import unicodedata

import requests

SRC = "data/finder/kr_business"
OUT = "data/finder/kr_tags"
INDEX = "data/finder/kr_tags_index.json"
CFG = json.load(open("config/finder_themes.json", encoding="utf-8"))
THEMES = {t["id"]: t for g in CFG["groups"] for t in g["themes"]}


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def theme_sheet():
    lines = []
    for g in CFG["groups"]:
        lines.append(f"[{g['name']}]")
        for t in g["themes"]:
            extra = f" (제외: {t['not']})" if t.get("not") else ""
            lines.append(f"- {t['id']}: {t['name']} — {t['def']}{extra}")
    return "\n".join(lines)


SYSTEM = f"""너는 한국 상장사의 사업보고서를 읽고 투자 테마를 분류하는 애널리스트다.
아래 테마 목록에서만 고른다. 해당 없으면 빈 목록이다. 회사당 최대 4개.

{theme_sheet()}

순도 판정:
- core: 해당 테마 매출이 전체의 50% 이상
- major: 20% 이상 50% 미만
- minor: 20% 미만이지만 사업으로 명시됨. 매출 전(개발·계획 단계)이면 minor, basis에 "매출 전"
매출 비중 표가 있으면 표 숫자로 판정(basis "표"), 없으면 서술로 판정(basis "서술").
연결 자회사 매출로 판정했으면 basis에 "연결 자회사"를 덧붙인다.

evidence는 원문에서 그대로 옮긴 짧은 구절(60자 이내)이다. 바꿔 쓰지 않는다.
summary는 이 회사가 무엇을 만들어 누구에게 파는지 35자 이내 한국어 한 줄.

JSON만 출력:
{{"summary": "...", "themes": [{{"id": "...", "purity": "core|major|minor", "share": 숫자 또는 null, "basis": "표|서술|...", "evidence": "..."}}]}}"""


def prompt(rec, budget):
    parts = [f"회사: {rec['name']} ({rec['code']}, {'코스피' if rec['cls'] == 'Y' else '코스닥'})"]
    for key, label, lim in (("products", "주요 제품 및 매출 비중", 0.45), ("overview", "사업의 개요", 0.40),
                            ("sales", "매출 및 수주", 0.15), ("fallback", "사업의 내용", 1.0)):
        if rec.get(key):
            parts.append(f"## {label}\n{rec[key][: int(budget * lim)]}")
    return "\n\n".join(parts)


class RateLimited(RuntimeError):
    pass


def call_anthropic(user):
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": os.environ.get("TAG_MODEL") or "claude-haiku-5-5", "max_tokens": 700,
              "system": SYSTEM, "messages": [{"role": "user", "content": user}]})
    if r.status_code == 429:
        raise RateLimited(r.text[:200])
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def call_github(user):
    r = requests.post("https://models.github.ai/inference/chat/completions", timeout=120, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json"},
        json={"model": os.environ.get("TAG_MODEL") or "openai/gpt-4.1-mini", "max_tokens": 700,
              "temperature": 0, "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]})
    if r.status_code == 429:
        raise RateLimited(r.text[:200])
    try:
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"HTTP {r.status_code} {r.headers.get('content-type')} {r.text[:300]!r}") from e


def norm(s):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", s or "")).lower()


def clean(raw, rec):
    m = re.search(r"\{.*\}", raw, re.S)
    js = json.loads(m.group(0))
    src = norm(" ".join(rec.get(k, "") for k in ("products", "overview", "sales", "fallback")))
    out = []
    for t in js.get("themes") or []:
        if t.get("id") not in THEMES or t.get("purity") not in ("core", "major", "minor"):
            continue
        ev = (t.get("evidence") or "")[:80]
        out.append({"id": t["id"], "purity": t["purity"], "share": t.get("share"),
                    "basis": t.get("basis"), "evidence": ev,
                    "ev_found": bool(ev) and norm(ev)[:25] in src})   # 근거가 원문에 실제로 있는지
    return {"summary": (js.get("summary") or "")[:60], "themes": out[:4]}


def main():
    if os.environ.get("ANTHROPIC_API_KEY"):
        call, budget, gap, prov = call_anthropic, 9000, 0.3, "anthropic"
    else:
        call, budget, gap, prov = call_github, 5500, 4.5, "github"
    deadline = time.time() + 60 * float(os.environ.get("DEADLINE_MIN") or 300)
    os.makedirs(OUT, exist_ok=True)
    todo = sorted(f[:-5] for f in os.listdir(SRC) if f.endswith(".json") and not os.path.exists(f"{OUT}/{f}"))
    if os.environ.get("MAX_COMPANIES"):
        todo = todo[: int(os.environ["MAX_COMPANIES"])]
    log(f"제공자 {prov}, 대상 {len(todo)}곳")
    stats = {"ok": 0, "empty_src": 0, "error": 0}
    errors = []
    for i, code in enumerate(todo, 1):
        if time.time() > deadline:
            log("시간 한도 도달")
            break
        rec = json.load(open(f"{SRC}/{code}.json", encoding="utf-8"))
        base = {"code": code, "name": rec["name"], "rcept_no": rec.get("rcept_no"), "model": prov}
        if not any(rec.get(k) for k in ("products", "overview", "fallback")):
            stats["empty_src"] += 1
            json.dump(dict(base, summary="", themes=[], note="사업의 내용 추출 실패"),
                      open(f"{OUT}/{code}.json", "w", encoding="utf-8"), ensure_ascii=False)
            continue
        try:
            res = clean(call(prompt(rec, budget)), rec)
            json.dump(dict(base, **res), open(f"{OUT}/{code}.json", "w", encoding="utf-8"), ensure_ascii=False)
            stats["ok"] += 1
        except RateLimited as e:
            log(f"한도 도달로 중단: {e}")
            break
        except Exception as e:  # noqa: BLE001
            stats["error"] += 1
            errors.append([code, rec["name"], f"{type(e).__name__}: {str(e)[:400]}"])
        if i % 50 == 0:
            log(f"{i}/{len(todo)} {stats}")
        time.sleep(gap)
    done = [f for f in os.listdir(OUT) if f.endswith(".json")]
    idx = {"asof": dt.date.today().isoformat(), "provider": prov, "tagged": len(done),
           "source": len([f for f in os.listdir(SRC) if f.endswith(".json")]), "this_run": stats, "errors": errors[:100]}
    json.dump(idx, open(INDEX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(json.dumps({k: v for k, v in idx.items() if k != "errors"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
