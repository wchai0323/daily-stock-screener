"""한국 시장 고유 규칙: KSIC 업종·섹터, 금융·스팩·리츠 제외, 중국 국적, 지주사, 메자닌, 거래소 지정 종목."""
from . import kr_config as K

# KSIC 중분류(2자리) → 섹터 이름
KSIC2 = {
    "01": "농업", "02": "임업", "03": "어업",
    "05": "석탄·원유·천연가스", "06": "금속광업", "07": "비금속광물 광업", "08": "광업 지원",
    "10": "식료품", "11": "음료", "12": "담배", "13": "섬유", "14": "의복", "15": "가죽·신발",
    "16": "목재", "17": "펄프·종이", "18": "인쇄", "19": "석유정제·코크스", "20": "화학",
    "21": "의약품·바이오", "22": "고무·플라스틱", "23": "비금속 광물제품", "24": "1차 금속",
    "25": "금속가공", "26": "반도체·전자부품·통신장비", "27": "의료·정밀·광학기기", "28": "전기장비",
    "29": "기계·장비", "30": "자동차·부품", "31": "조선·항공·기타 운송장비", "32": "가구",
    "33": "기타 제조", "34": "산업용 기계 수리",
    "35": "전기·가스", "36": "수도", "37": "하수·폐수", "38": "폐기물 처리", "39": "환경 정화",
    "41": "종합 건설", "42": "전문 건설",
    "45": "자동차 판매", "46": "도매", "47": "소매",
    "49": "육상 운송", "50": "해운", "51": "항공 운송", "52": "창고·운송 서비스",
    "55": "숙박", "56": "음식점",
    "58": "출판·소프트웨어", "59": "영상·음악 콘텐츠", "60": "방송", "61": "통신",
    "62": "IT 서비스·SI", "63": "정보 서비스·플랫폼",
    "64": "금융·지주", "65": "보험", "66": "금융 보조",
    "68": "부동산",
    "70": "연구개발", "71": "전문 서비스", "72": "엔지니어링", "73": "기타 전문 서비스",
    "74": "사업시설 관리", "75": "사업 지원 서비스", "76": "임대",
    "85": "교육", "86": "보건", "87": "사회복지",
    "90": "창작·예술", "91": "스포츠·오락",
    "94": "협회·단체", "95": "수리", "96": "개인 서비스",
}

CHINA_WORDS = ("중국", "중화인민공화국", "홍콩", "CHINA", "HONG KONG", "PRC")


def _ksic(c):
    k = str(c.get("ksic") or "").strip()
    return k if k.isdigit() else ""


def sector_of(c):
    k = _ksic(c)
    if c.get("is_holding"):
        return "지주회사"
    return KSIC2.get(k[:2]) if k else None


def industry_keys(c):
    k = _ksic(c)
    if not k:
        return None, None
    return f"KSIC{k[:3]}", f"KSIC{k[:2]}"


def is_cyclical(c):
    k = _ksic(c)
    return bool(k) and k.startswith(K.C_EXCLUDED_KSIC_PREFIXES)


def is_holding(ksic, name):
    name = name or ""
    return ksic == K.HOLDING_KSIC or name.endswith("홀딩스") or name.endswith("지주")


def is_china(address, name):
    a = (address or "").upper()
    return any(w in a for w in CHINA_WORDS) or "차이나" in (name or "")


def universe_extra(c):
    r = []
    if c.get("market") not in ("KOSPI", "KOSDAQ"):
        r.append("코스피·코스닥 외 시장")
    k = _ksic(c)
    name = c.get("name") or ""
    if k.startswith(K.EXCLUDED_KSIC_PREFIXES) and not c.get("is_holding"):
        r.append("금융업")
    if "스팩" in name or "기업인수목적" in name:
        r.append("스팩")
    if "리츠" in name or "REIT" in name.upper() or "부동산투자회사" in name:
        r.append("리츠")
    if c.get("is_china"):
        r.append("H7 중국 국적·본사")
    if c.get("fiscal_month") not in (None, 12):
        r.append("12월 결산 아님 (분기 계산 미지원)")
    return r


def extra_hard(c):
    r = []
    flags = c.get("risk_flags") or []
    if flags:
        r.append("거래소 지정: " + ", ".join(flags))
    mz = c.get("mezz_ratio")
    if mz is not None and mz > K.MEZZ_MAX:
        r.append(f"H1 CB·BW 잔액 시총의 {mz:.0%} (잠재 희석)")
    return r


def extra_tags(c):
    tags = []
    if c.get("is_holding"):
        tags.append("지주사")
    mz = c.get("mezz_ratio")
    if mz is not None and mz >= K.MEZZ_TAG:
        tags.append(f"CB·BW 잔액 시총의 {mz:.0%}")
    if c.get("fs_div") == "OFS":
        tags.append("별도재무")
    return tags


def krw(v):
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.1f}조원"
    if a >= 1e8:
        return f"{v / 1e8:,.0f}억원"
    return f"{v / 1e4:,.0f}만원"
