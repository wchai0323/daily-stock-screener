"""시장별 설정(기준값 + 시장 고유 규칙). 필터·점수·메일 코드는 이 프로필만 보고 동작한다.

미국: config.py + SEC SIC 분류 / 한국: kr_config.py + KSIC 분류·DART 위험 지표
"""
import types

from . import config as US_C
from . import sectors


def _consts(mod):
    return {k: getattr(mod, k) for k in dir(mod) if k.isupper()}


# ---------------- 미국 ----------------

def _us_excluded_sic(sic):
    if sic is None:
        return False
    if sic in US_C.EXCLUDED_SIC_CODES:
        return True
    return any(lo <= sic <= hi for lo, hi in US_C.EXCLUDED_SIC_RANGES)


def _us_universe_extra(c):
    r = []
    if c.get("exchange") not in US_C.EXCHANGES:
        r.append("거래소")
    if _us_excluded_sic(c.get("sic")):
        r.append("금융·REIT·SPAC 업종")
    if c.get("country") in US_C.CHINA_LOCATION_CODES:
        r.append("H7 중국 연관(본사 주소)")
    return r


def _us_industry_keys(c):
    sic = c.get("sic")
    if sic is None:
        return None, None
    return f"SIC{sic // 10:03d}", f"SIC{sic // 100:02d}"


def _us_cyclical(c):
    sic = c.get("sic")
    return sic is not None and any(lo <= sic <= hi for lo, hi in US_C.C_EXCLUDED_SIC_RANGES)


def usd(v):
    if v is None:
        return "–"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.1f}B"
    return f"${v / 1e6:.0f}M"


US = types.SimpleNamespace(
    **_consts(US_C),
    NAME="US",
    TITLE="일일 주식 스크리너",
    SUBJECT="[스크리너]",
    CAP_BANDS=[(2e9, 6), (5e9, 4), (10e9, 2)],     # 시총 미만 → 점수 (그 외 1점)
    USE_SHORT=True,
    INDUSTRY_LABEL="같은 업종(SIC)",
    SOURCE_NOTE="재무는 SEC 10-Q/10-K 공시 기준이라 실적 발표 후 공시까지 며칠~수주 늦을 수 있음",
    VALUATION_NOTE=("TTM PER·PSR은 SEC 재무로 계산, Forward PER·PEG는 Yahoo 컨센서스(조회 실패 시 –). "
                    "섹터·업종은 SEC 업종 분류(SIC)."),
    universe_extra=_us_universe_extra,
    extra_hard=lambda c: [],
    extra_tags=lambda c: [],
    industry_keys=_us_industry_keys,
    is_cyclical=_us_cyclical,
    fmt_money=usd,
    sector_of=lambda c: sectors.sector_of(c.get("sic")),
)


def korea():
    """한국 프로필은 필요할 때만 만든다 (미국 실행에 영향 없음)."""
    from . import kr_config as K
    from . import kr_rules as R

    return types.SimpleNamespace(
        **_consts(K),
        NAME="KR",
        TITLE="한국 주식 스크리너",
        SUBJECT="[한국 스크리너]",
        USE_SHORT=False,
        INDUSTRY_LABEL="같은 업종(KSIC)",
        SOURCE_NOTE="재무는 DART 정기보고서 기준이라 잠정실적 공시보다 늦게 반영됨",
        VALUATION_NOTE=("TTM PER·PSR은 DART 재무로 계산, Forward PER·PEG는 Yahoo 컨센서스(대부분 대형주만, 조회 실패 시 –). "
                        "섹터·업종은 KSIC 분류."),
        universe_extra=R.universe_extra,
        extra_hard=R.extra_hard,
        extra_tags=R.extra_tags,
        industry_keys=R.industry_keys,
        is_cyclical=R.is_cyclical,
        fmt_money=R.krw,
        sector_of=R.sector_of,
    )
