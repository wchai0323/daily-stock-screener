"""전일 대비 변동 계산과 메일 본문(HTML) 작성."""
import html
import math

import pandas as pd

from . import config as C

RESULT_COLS = [
    "ticker", "name", "sector", "industry", "lane", "auto_score", "auto_score_pct", "priority", "mkt_cap",
    "pe_ttm", "pe_fwd", "peg", "psr", "rev_growth", "q_yoy",
    "accel", "gpm", "ind_gpm_median", "opm", "opm_change", "fcf_margin", "fcf_yield", "dilution",
    "net_debt_ebitda", "runway_months", "rule_of_40", "short_pct_float", "price", "range_pos", "ma200_gap",
    "entry_tags", "period_end", "latest_filed", "ind_group", "sic",
    "S1", "S2", "S3", "S5", "S6", "S7", "S8", "S9", "S10", "S12", "S13", "S18s",
]
SNAPSHOT_COLS = [
    "ticker", "cik", "name", "exchange", "sic", "country", "mkt_cap", "price", "adv20", "revenue_ttm",
    "rev_growth", "q_yoy", "accel", "gpm", "gpm_prev", "gp_growth", "opm", "opm_change", "fcf_ttm",
    "fcf_yield", "dilution", "net_debt", "net_debt_ebitda", "runway_months", "op_asset_growth",
    "gp_to_assets", "period_end", "pass", "lane", "auto_score", "fail",
]


def to_frame(rows, cols):
    recs = []
    for c in rows:
        r = {k: c.get(k) for k in cols}
        for k, v in (c.get("scores") or {}).items():
            if k in cols:
                r[k] = v
        if "fail" in r and isinstance(r["fail"], list):
            r["fail"] = "; ".join(r["fail"])
        if "entry_tags" in r and isinstance(r["entry_tags"], list):
            r["entry_tags"] = ", ".join(r["entry_tags"])
        recs.append(r)
    return pd.DataFrame(recs, columns=cols)


def diff(today_pass, prev_df, all_today):
    """신규 진입·이탈. today_pass: 통과 종목 dict 목록, prev_df: 전일 결과 CSV."""
    today_set = {c["ticker"] for c in today_pass}
    if prev_df is None:
        return None, None
    prev_set = set(prev_df["ticker"].astype(str))
    by_ticker = {c["ticker"]: c for c in all_today}
    new = [c for c in today_pass if c["ticker"] not in prev_set]
    exits = []
    for t in sorted(prev_set - today_set):
        c = by_ticker.get(t)
        exits.append({"ticker": t, "name": (c or {}).get("name") or "",
                      "reasons": (c or {}).get("fail") or ["오늘 데이터 없음 (상장 변경·데이터 오류 가능)"]})
    return new, exits


# ---------------- HTML ----------------

def _pct(v, signed=False, digits=0):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "–"
    return f"{v:+.{digits}%}" if signed else f"{v:.{digits}%}"


def _cap(v):
    if not v:
        return "–"
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _x(v, digits=1):
    """배수 표시 (PER·PEG·PSR). 없으면 –, 음수·비정상 값도 –."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) or v <= 0:
        return "–"
    return f"{v:,.0f}" if v >= 100 else f"{v:.{digits}f}"


def _valuation(c):
    """PER TTM / Fwd, PEG, PSR를 한 칸에. TTM PER이 없고 영업적자면 '적자'."""
    pe_ttm = _x(c.get("pe_ttm"))
    if pe_ttm == "–" and (c.get("opm") is not None and c["opm"] < 0):
        pe_ttm = "적자"
    return (f"PER {pe_ttm} / {_x(c.get('pe_fwd'))}<br>"
            f"<span style='color:#666'>PEG {_x(c.get('peg'), 2)} · PSR {_x(c.get('psr'))}</span>")


def _class(c):
    sec, ind = c.get("sector"), c.get("industry")
    if not sec and not ind:
        return "–"
    return f"{_e(sec or '–')}<br><span style='color:#666'>{_e(ind or '')}</span>"


TABLE = ('style="border-collapse:collapse;width:100%;font-size:12px;margin:6px 0 18px;'
         'font-family:Malgun Gothic,Apple SD Gothic Neo,Arial,sans-serif"')
TH = 'style="background:#DCE3EE;border:1px solid #B8C2D1;padding:5px 6px;text-align:left;white-space:nowrap"'
TD = 'style="border:1px solid #D5DBE5;padding:4px 6px;vertical-align:top"'
H2 = 'style="font-size:15px;color:#1F3864;margin:22px 0 4px;border-bottom:2px solid #1F3864;padding-bottom:3px"'


def _table(headers, rows):
    out = [f"<table {TABLE}><tr>" + "".join(f"<th {TH}>{_e(h)}</th>" for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td {TD}>{c}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _summary_line(c):
    bits = [f"{c['lane']}차선", f"매출 {_pct(c.get('rev_growth'), True)}"]
    if c.get("accel") is not None:
        bits.append(f"가속 {c['accel'] * 100:+.0f}%p")
    bits.append(f"GPM {_pct(c.get('gpm'))}")
    if c["lane"] == "C" and c.get("fcf_yield") is not None:
        bits.append(f"FCF수익률 {_pct(c['fcf_yield'], digits=1)}")
    return " · ".join(bits)


def _cand_rows(cands, holdings):
    rows = []
    for c in cands:
        star = "★ " if c.get("priority") else ""
        held = " (보유)" if c["ticker"] in holdings else ""
        rows.append([
            f"<b>{star}{_e(c['ticker'])}</b>{held}<br><span style='color:#666'>{_e((c.get('name') or '')[:28])}</span>",
            _class(c),
            _e(c["lane"]),
            f"<b>{c.get('auto_score', 0):.0f}</b>/{C.AUTO_SCORE_MAX}",
            _cap(c.get("mkt_cap")),
            _valuation(c),
            _pct(c.get("rev_growth"), True),
            "–" if c.get("accel") is None else f"{c['accel'] * 100:+.0f}%p",
            (f"{_pct(c.get('gpm'))}<br>" if c.get('gpm') is not None else "미공시<br>") + f"<span style='color:#888'>업종 {_pct(c.get('ind_gpm_median'))}</span>",
            _pct(c.get("opm"), True),
            _pct(c.get("dilution"), True, 1),
            _e(", ".join(c.get("entry_tags") or [])) or "–",
        ])
    return rows


CAND_HEAD = ["종목", "섹터 / 업종", "차선", "자동점수", "시총", "PER TTM/Fwd · PEG · PSR", "매출 YoY", "가속",
             "매출총이익률", "영업이익률", "희석", "진입 참고"]


def render(day, prev_day, passed, new, exits, holdings, all_today, stats):
    passed = sorted(passed, key=lambda c: -(c.get("auto_score") or 0))
    by_ticker = {c["ticker"]: c for c in all_today}
    parts = [f"<div style='font-family:Malgun Gothic,Apple SD Gothic Neo,Arial,sans-serif;color:#222;max-width:960px'>"]
    parts.append(f"<h1 style='font-size:20px;color:#1F3864;margin:0'>일일 주식 스크리너 · {day}</h1>")
    n_g = sum(1 for c in passed if c["lane"] == "G")
    n_c = len(passed) - n_g
    n_pri = sum(1 for c in passed if c.get("priority"))
    line = (f"유니버스 {stats['universe']:,}개 중 <b>{len(passed)}개 통과</b> (G {n_g} · C {n_c}), "
            f"1순위 후보(자동점수 {C.PRIORITY_SCORE_PCT:.0%} 이상) <b>{n_pri}개</b>.")
    if new is not None:
        line += f" 전일({prev_day}) 대비 신규 <b>{len(new)}</b> · 이탈 <b>{len(exits)}</b>."
    else:
        line += " 첫 실행이라 전일 비교가 없습니다."
    parts.append(f"<p style='margin:6px 0 2px'>{line}</p>")
    parts.append(f"<p style='margin:0;color:#777;font-size:11px'>오늘 재무 갱신 {stats['refreshed']}개 기업 · "
                 f"재무는 SEC 10-Q/10-K 공시 기준이라 실적 발표 후 공시까지 며칠~수주 늦을 수 있음 · "
                 f"자동점수는 2층 100점 중 자동 계산 가능한 {C.AUTO_SCORE_MAX}점 기준</p>")

    # 보유 종목 경고
    held_alerts = []
    for t in sorted(holdings):
        c = by_ticker.get(t)
        if c is None:
            continue
        if not c.get("pass"):
            held_alerts.append([f"<b>{_e(t)}</b>", "<span style='color:#C00000'>필터 미통과</span>",
                                _e("; ".join(c.get("fail") or []))])
    if held_alerts:
        parts.append(f"<h2 {H2}>⚠ 보유 종목 점검</h2>")
        parts.append("<p style='margin:2px 0;font-size:12px'>보유 종목 중 오늘 1층을 통과하지 못한 종목입니다. "
                     "반증 조건(P4)과 대조해 재심사하세요.</p>")
        parts.append(_table(["종목", "상태", "걸린 조건"], held_alerts))

    if new is not None:
        parts.append(f"<h2 {H2}>신규 진입 ({len(new)})</h2>")
        if new:
            rows = [[f"<b>{_e(c['ticker'])}</b> {_e((c.get('name') or '')[:28])}",
                     f"{c.get('auto_score', 0):.0f}/{C.AUTO_SCORE_MAX}", _e(_summary_line(c))]
                    for c in sorted(new, key=lambda c: -(c.get('auto_score') or 0))[:C.MAX_ROWS_PER_SECTION]]
            parts.append(_table(["종목", "자동점수", "진입 근거"], rows))
        else:
            parts.append("<p style='font-size:12px;color:#666'>없음</p>")
        parts.append(f"<h2 {H2}>이탈 ({len(exits)})</h2>")
        if exits:
            rows = [[f"<b>{_e(x['ticker'])}</b>" + (" <span style='color:#C00000'>(보유)</span>" if x["ticker"] in holdings else ""),
                     _e(x["name"][:28]), _e("; ".join(x["reasons"]))] for x in exits[:C.MAX_ROWS_PER_SECTION]]
            parts.append(_table(["종목", "이름", "걸린 조건"], rows))
        else:
            parts.append("<p style='font-size:12px;color:#666'>없음</p>")

    parts.append(f"<h2 {H2}>자동점수 상위 {min(C.TOP_N, len(passed))}</h2>")
    parts.append("<p style='margin:2px 0;font-size:11px;color:#666'>★ = 1순위 후보. 업종 = 같은 업종(SIC) 매출총이익률 40백분위(G 차선 흑자 기준선). "
                 "진입 참고는 점수가 아닌 분할매수 속도 참고용.</p>")
    parts.append(_table(CAND_HEAD, _cand_rows(passed[:C.TOP_N], holdings)))
    parts.append("<p style='font-size:11px;color:#777'>전체 통과 종목과 항목별 점수는 첨부 CSV에 있습니다. "
                 "남은 2층 31점(잔고·역DCF·TAM·경영진·내부자·가이던스·기관)과 3·4층은 체크리스트 검증 단계에서 채웁니다.</p>")
    parts.append("</div>")
    return "".join(parts)
