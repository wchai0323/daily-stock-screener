"""세션에서 판정한 태그(JSONL: {"code","summary","themes":[...]})를 검증해 kr_tags에 저장한다."""
import json
import sys

sys.path.insert(0, "tools")
from finder_tag import OUT, clean  # noqa: E402

n = bad = 0
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    js = json.loads(line)
    rec = json.load(open(f"data/finder/kr_business/{js['code']}.json", encoding="utf-8"))
    res = clean(json.dumps(js, ensure_ascii=False), rec)
    out = dict(code=js["code"], name=rec["name"], rcept_no=rec.get("rcept_no"), model="claude-session", **res)
    json.dump(out, open(f"{OUT}/{js['code']}.json", "w", encoding="utf-8"), ensure_ascii=False)
    n += 1
    miss = [t["id"] for t in res["themes"] if not t["ev_found"]]
    if miss:
        bad += 1
        print("근거 불일치", js["code"], rec["name"], miss)
print(f"저장 {n}곳, 근거 불일치 {bad}곳")
