"""DART OpenAPI 수집기.

DART 정기보고서(1분기·반기·3분기·사업보고서)의 재무제표를 SEC 압축본과 같은 형식
({개념 태그: [[시작일, 종료일, 값, 공시일, 접수번호], ...]})으로 바꿔서,
분기 계산(facts.build_series)·지표(metrics)·필터(screen)를 미국과 똑같이 쓴다.

API 문서: https://opendart.fss.or.kr/guide/main.do
"""
import datetime as dt
import io
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

BASE = "https://opendart.fss.or.kr/api/"

# reprt_code → (분기 번호, 분기말 월-일). 12월 결산 기준
REPORTS = {"11013": (1, "03-31"), "11012": (2, "06-30"), "11014": (3, "09-30"), "11011": (4, "12-31")}
MONTH_TO_REPORT = {"03": "11013", "06": "11012", "09": "11014", "12": "11011"}
Q_START = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}


class DartError(RuntimeError):
    pass


class BudgetExceeded(DartError):
    pass


class DartClient:
    def __init__(self, api_key=None, budget=15000, min_interval=0.1):
        self.key = api_key or os.environ.get("DART_API_KEY")
        if not self.key:
            raise DartError("DART_API_KEY 환경변수가 필요합니다")
        self.budget = budget
        self.count = 0
        self.min_interval = min_interval
        self._last = 0.0
        self.session = requests.Session()

    def _get(self, path, **params):
        if self.count >= self.budget:
            raise BudgetExceeded(f"DART 호출 예산 {self.budget}건 소진")
        params["crtfc_key"] = self.key
        for attempt in range(4):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.count += 1
            try:
                r = self.session.get(BASE + path, params=params, timeout=60)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r
            time.sleep(2 ** (attempt + 1))
        raise DartError(f"{path} 요청 실패")

    def _json(self, path, **params):
        js = self._get(path, **params).json()
        status = js.get("status")
        if status == "000":
            return js
        if status == "013":          # 조회된 데이터 없음
            return None
        if status == "020":          # 하루 한도 초과
            raise BudgetExceeded("DART 하루 요청 한도 초과 (020)")
        raise DartError(f"{path} 오류 {status}: {js.get('message')}")

    # ---- 기업 목록 ----
    def corp_codes(self):
        """상장사 목록: [{'corp_code', 'corp_name', 'stock_code'}]."""
        r = self._get("corpCode.xml")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml = z.read(z.namelist()[0])
        out = []
        for el in ET.fromstring(xml).iter("list"):
            stock = (el.findtext("stock_code") or "").strip()
            if re.fullmatch(r"\d{6}", stock):
                out.append({"corp_code": el.findtext("corp_code").strip(),
                            "corp_name": (el.findtext("corp_name") or "").strip(), "stock_code": stock})
        return out

    def company(self, corp_code):
        return self._json("company.json", corp_code=corp_code)

    def financials(self, corp_code, year, reprt_code, fs_div):
        js = self._json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(year),
                        reprt_code=reprt_code, fs_div=fs_div)
        return (js or {}).get("list")

    def shares(self, corp_code, year, reprt_code):
        js = self._json("stockTotqySttus.json", corp_code=corp_code, bsns_year=str(year), reprt_code=reprt_code)
        return parse_common_shares((js or {}).get("list"))

    def filings(self, bgn: dt.date, end: dt.date, pblntf_ty, detail_ty=None):
        """공시 검색 (기업 미지정 시 3개월 이내). 모든 페이지를 모아 돌려준다."""
        out, page = [], 1
        while True:
            params = dict(bgn_de=bgn.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
                          pblntf_ty=pblntf_ty, page_no=page, page_count=100)
            if detail_ty:
                params["pblntf_detail_ty"] = detail_ty
            js = self._json("list.json", **params)
            if not js:
                break
            out.extend(js.get("list") or [])
            if page >= int(js.get("total_page") or 1):
                break
            page += 1
        return out


# ---------------- 보고서 일정 ----------------

def report_key(year, reprt_code):
    return f"{year}-{reprt_code}"


def period_end(year, reprt_code):
    return f"{year}-{REPORTS[reprt_code][1]}"


def recent_reports(today: dt.date, n):
    """오늘 기준 최근 n개 분기 보고서 (분기말이 14일 이상 지난 것만), 오래된 순."""
    out = []
    y, q = today.year, (today.month - 1) // 3 + 1
    while len(out) < n:
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        code = [k for k, v in REPORTS.items() if v[0] == q][0]
        if dt.date.fromisoformat(period_end(y, code)) + dt.timedelta(days=14) <= today:
            out.append((y, code))
    return list(reversed(out))


def due_date(year, reprt_code):
    """법정 제출 기한: 분기·반기 45일, 사업보고서 90일."""
    end = dt.date.fromisoformat(period_end(year, reprt_code))
    return end + dt.timedelta(days=90 if reprt_code == "11011" else 45)


def parse_report_name(report_nm):
    """'[기재정정]분기보고서 (2026.09)' → (2026, '11014', 정정 여부). 정기보고서가 아니면 None."""
    if not any(k in report_nm for k in ("분기보고서", "반기보고서", "사업보고서")):
        return None
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if not m or m.group(2) not in MONTH_TO_REPORT:
        return None
    return int(m.group(1)), MONTH_TO_REPORT[m.group(2)], "정정" in report_nm


# ---------------- 재무제표 파싱 ----------------

def _amount(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(name):
    s = re.sub(r"\s+", "", name or "")
    s = re.sub(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX\d]+[.)]", "", s)   # 'Ⅰ.매출액', '1.매출원가' 같은 번호 제거
    return s.replace("(손실)", "").replace("(손익)", "")


# 개념별 (account_id 집합, 계정명 집합). 첫 번째로 맞는 행 하나만 쓰는 개념
SINGLE = {
    "revenue": ({"ifrs-full_Revenue", "ifrs_Revenue"}, {"매출액", "수익(매출액)", "영업수익", "매출", "수익"}),
    "cogs": ({"ifrs-full_CostOfSales"}, {"매출원가"}),
    "gross_profit": ({"ifrs-full_GrossProfit"}, {"매출총이익"}),
    "op_income": ({"dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"}, {"영업이익"}),
    "ni_owner": ({"ifrs-full_ProfitLossAttributableToOwnersOfParent"}, set()),
    "ni_total": ({"ifrs-full_ProfitLoss"}, {"당기순이익", "분기순이익", "반기순이익", "연결당기순이익", "당기순손익"}),
    "ocf": ({"ifrs-full_CashFlowsFromUsedInOperatingActivities"}, {"영업활동현금흐름", "영업활동으로인한현금흐름"}),
    "capex": ({"ifrs-full_PurchaseOfPropertyPlantAndEquipment"}, {"유형자산의취득"}),
    "assets": ({"ifrs-full_Assets"}, {"자산총계"}),
    "cash": ({"ifrs-full_CashAndCashEquivalents"}, {"현금및현금성자산"}),
}
# 여러 행을 합치는 개념
SUMMED = {
    "da": ({"ifrs-full_AdjustmentsForDepreciationExpense", "ifrs-full_AdjustmentsForAmortisationExpense",
            "ifrs-full_AdjustmentsForDepreciationAndAmortisationExpense"},
           {"감가상각비", "무형자산상각비", "사용권자산상각비", "감가상각비및무형자산상각비"}),
    "st_investments": ({"ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents",
                        "dart_ShortTermDepositsNotClassifiedAsCashEquivalents"},
                       {"단기금융상품", "단기투자자산", "단기금융자산"}),
    "debt": ({"ifrs-full_ShorttermBorrowings", "ifrs-full_LongtermBorrowings",
              "ifrs-full_CurrentPortionOfLongtermBorrowings"},
             {"단기차입금", "장기차입금", "유동성장기차입금", "유동성장기부채", "사채", "유동성사채", "장기사채",
              "전환사채", "유동성전환사채", "비유동전환사채", "신주인수권부사채", "유동성신주인수권부사채",
              "교환사채", "유동성교환사채"}),
}
MEZZ_WORDS = ("전환사채", "신주인수권부사채", "교환사채")
MEZZ_EXCLUDE = ("할인", "조정", "할증", "상환", "평가", "이자")

# 압축본 태그 (facts.DURATION_CONCEPTS / INSTANT_CONCEPTS의 첫 태그와 같게)
TAG = {
    "revenue": "Revenues", "cogs": "CostOfRevenue", "gross_profit": "GrossProfit",
    "op_income": "OperatingIncomeLoss", "net_income": "NetIncomeLoss",
    "ocf": "NetCashProvidedByUsedInOperatingActivities", "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "da": "DepreciationDepletionAndAmortization", "shares": "WeightedAverageNumberOfDilutedSharesOutstanding",
    "assets": "Assets", "cash": "CashAndCashEquivalentsAtCarryingValue", "st_investments": "ShortTermInvestments",
    "debt": "LongTermDebt", "mezz": "MezzanineDebt",
}


def _match(row, ids, names):
    return row.get("account_id") in ids or _norm(row.get("account_nm")) in names


def parse_statement(rows):
    """fnlttSinglAcntAll 목록 → {개념: {'q': 당분기/당기 값, 'ytd': 누적 값}} + 메자닌 합계.

    손익(IS/CIS): q=당분기 3개월(사업보고서는 연간), ytd=누적 / 현금흐름(CF): ytd만 / 재무상태(BS): q=시점 값
    """
    by_sj = {}
    for r in rows or []:
        by_sj.setdefault(r.get("sj_div"), []).append(r)
    is_rows = by_sj.get("IS", []) + by_sj.get("CIS", [])
    cf_rows = by_sj.get("CF", [])
    bs_rows = by_sj.get("BS", [])
    out = {}

    def first(rows_, concept):
        ids, names = SINGLE[concept]
        for r in rows_:
            if _match(r, ids, names):
                return r
        return None

    for concept in ("revenue", "cogs", "gross_profit", "op_income", "ni_owner", "ni_total"):
        r = first(is_rows, concept)
        if r:
            out[concept] = {"q": _amount(r.get("thstrm_amount")), "ytd": _amount(r.get("thstrm_add_amount"))}
    for concept in ("ocf", "capex"):
        r = first(cf_rows, concept)
        if r:
            v = _amount(r.get("thstrm_amount"))
            out[concept] = {"ytd": abs(v) if (concept == "capex" and v is not None) else v}
    for concept in ("assets", "cash"):
        r = first(bs_rows, concept)
        if r:
            out[concept] = {"q": _amount(r.get("thstrm_amount"))}

    def summed(rows_, concept, absval=False):
        ids, names = SUMMED[concept]
        vals = [_amount(r.get("thstrm_amount")) for r in rows_ if _match(r, ids, names)]
        vals = [abs(v) if absval else v for v in vals if v is not None]
        return sum(vals) if vals else None

    da = summed(cf_rows, "da", absval=True)
    if da is not None:
        out["da"] = {"ytd": da}
    for concept in ("st_investments", "debt"):
        v = summed(bs_rows, concept)
        if v is not None:
            out[concept] = {"q": v}
    mezz = [_amount(r.get("thstrm_amount")) for r in bs_rows
            if any(w in _norm(r.get("account_nm")) for w in MEZZ_WORDS)
            and not any(x in _norm(r.get("account_nm")) for x in MEZZ_EXCLUDE)]
    mezz = [v for v in mezz if v is not None and v > 0]
    out["mezz"] = {"q": sum(mezz) if mezz else 0.0}
    # 순이익: 지배주주 귀속을 우선
    ni = out.get("ni_owner") or out.get("ni_total")
    if ni:
        out["net_income"] = ni
    out.pop("ni_owner", None)
    out.pop("ni_total", None)
    return out


def parse_common_shares(rows):
    """stockTotqySttus 목록 → 보통주 발행주식총수."""
    for r in rows or []:
        if (r.get("se") or "").replace(" ", "") in ("보통주", "보통주식"):
            v = _amount(r.get("istc_totqy"))
            if v:
                return v
    return None


def to_compact_rows(year, reprt_code, parsed, rcept_no, shares=None):
    """한 보고서의 파싱 결과 → 압축본 행 {태그: [[시작, 종료, 값, 공시일, 접수번호]]}."""
    q, md = REPORTS[reprt_code]
    end = f"{year}-{md}"
    q_start = f"{year}-{Q_START[q]}"
    fy_start = f"{year}-01-01"
    filed = f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}" if rcept_no and len(rcept_no) >= 8 else None
    rows = {}

    def add(concept, start, val):
        if val is None:
            return
        rows.setdefault(TAG[concept], []).append([start, end, val, filed, rcept_no])

    for concept in ("revenue", "cogs", "gross_profit", "op_income", "net_income"):
        v = parsed.get(concept)
        if not v:
            continue
        if reprt_code == "11011":
            add(concept, fy_start, v.get("q"))           # 연간
        else:
            add(concept, q_start, v.get("q"))            # 당분기 3개월
            if q > 1:
                add(concept, fy_start, v.get("ytd"))     # 누적
    for concept in ("ocf", "capex", "da"):
        v = parsed.get(concept)
        if v:
            add(concept, fy_start, v.get("ytd"))
    for concept in ("assets", "cash", "st_investments", "debt", "mezz"):
        v = parsed.get(concept)
        if v:
            add(concept, None, v.get("q"))
    if shares:
        add("shares", q_start, shares)
    return rows
