"""로컬 캐시(기업별 재무 압축본·메타데이터)와 날짜별 결과 저장.

- data/cache/      : GitHub Actions cache로 보존 (커밋하지 않음)
- data/results/    : 날짜별 통과 종목 CSV (커밋해서 기록으로 남김)
- data/snapshots/  : 날짜별 유니버스 전체 지표 (gzip, 커밋) — 나중에 필터 효과를 사후 검증하는 원자료
"""
import datetime as dt
import gzip
import json
import os
from pathlib import Path

import pandas as pd

ROOT = Path(os.environ.get("SCREENER_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
CACHE = ROOT / "cache" / "companies"
STATE = ROOT / "cache" / "state.json"
RESULTS = ROOT / "results"
SNAPSHOTS = ROOT / "snapshots"


def _ensure():
    for p in (CACHE, RESULTS, SNAPSHOTS):
        p.mkdir(parents=True, exist_ok=True)


def load_company(cik):
    p = CACHE / f"{int(cik)}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_company(cik, obj):
    _ensure()
    p = CACHE / f"{int(cik)}.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def cached_ciks():
    if not CACHE.exists():
        return set()
    return {int(p.name.split(".")[0]) for p in CACHE.glob("*.json.gz")}


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def save_state(state):
    _ensure()
    STATE.write_text(json.dumps(state, indent=1))


def save_results(day: dt.date, df: pd.DataFrame):
    _ensure()
    path = RESULTS / f"{day.isoformat()}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def save_snapshot(day: dt.date, df: pd.DataFrame):
    _ensure()
    path = SNAPSHOTS / f"{day.isoformat()}.csv.gz"
    df.to_csv(path, index=False, compression="gzip")
    return path


def previous_results(day: dt.date):
    """오늘보다 앞선 가장 최근 결과 파일."""
    if not RESULTS.exists():
        return None, None
    files = sorted(p for p in RESULTS.glob("*.csv") if p.stem < day.isoformat())
    if not files:
        return None, None
    return files[-1].stem, pd.read_csv(files[-1])


def load_holdings():
    p = Path(__file__).resolve().parent.parent / "config" / "holdings.txt"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip().upper()
        if line:
            out.add(line)
    return out
