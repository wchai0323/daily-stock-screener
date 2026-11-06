"""세션 태깅용: 회사별 사업의 내용을 짧게 묶어 출력한다.  python tools/finder_digest.py 005930 000660 ..."""
import json
import sys

LIM = (("products", "제품·매출비중", 1300), ("overview", "개요", 700), ("fallback", "사업의 내용", 1800))
for code in sys.argv[1:]:
    r = json.load(open(f"data/finder/kr_business/{code}.json", encoding="utf-8"))
    print(f"\n===== {code} {r['name']} ({'코스피' if r['cls'] == 'Y' else '코스닥'})")
    for k, label, n in LIM:
        if r.get(k):
            print(f"[{label}] {r[k][:n]}")
