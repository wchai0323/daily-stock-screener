"""스크리닝 기준값. '성장주 투자 프레임워크 v2' 문서의 0단계·1층·2층(자동 항목) 기준과 1:1로 대응한다.

기준을 바꿀 때는 이 파일만 고치고, 변경 이력을 CHANGELOG.md에 남긴다.
"""

# ---------- 0단계: 유니버스 ----------
EXCHANGES = {"NYSE", "Nasdaq"}
MKT_CAP_MIN = 300e6          # 3억 달러
MKT_CAP_MAX = 20e9           # 200억 달러 (H3과 동일)
REVENUE_TTM_MIN = 50e6       # TTM 매출 5천만 달러
ADV20_MIN = 5e6              # 20일 평균 거래대금 500만 달러
PRICE_MIN = 3.0              # 주가 3달러
MIN_QUARTERS = 8             # TTM YoY 계산에 필요한 최소 분기 수

# 제외 업종 (SIC): 은행·신용·증권·보험(6000~6499), SPAC(6770), REIT(6798)
EXCLUDED_SIC_RANGES = [(6000, 6499)]
EXCLUDED_SIC_CODES = {6770, 6798}

# 중국 연관 자동 판정: SEC 주소 코드 F4=중국, K3=홍콩, L2=마카오
CHINA_LOCATION_CODES = {"F4", "K3", "L2"}

# ---------- 1층: 하드 필터 ----------
H1_DILUTION_MAX = 0.05            # 희석 주식 수 1년 증가율 5%
H2_NET_DEBT_EBITDA_MAX = 2.0      # 흑자 기업 순부채/EBITDA
H2_RUNWAY_MONTHS_MIN = 24         # 적자 기업 현금 런웨이
H5_OPM_IMPROVEMENT_MIN = 0.05     # 적자 기업 영업이익률 1년 개선폭 5%p

# H4 G 차선 (빠른 경로)
G_REV_GROWTH_MIN = 0.25
G_ACCEL_FLOOR = -0.05             # 최근 분기 YoY - 직전 2분기 평균 > -5%p
G_GPM_FLOOR_PROFITABLE = 0.20     # 업종 40백분위 이상 + 절대 바닥 20%
G_GPM_PROFITABLE_PCTL = 0.40      # 2026-10-19: 업종 중앙값(50) → 40백분위로 완화
G_GPM_FLOOR_UNPROFITABLE = 0.25   # 업종 상위 30% + 절대 바닥 25%
G_GPM_UNPROFITABLE_PCTL = 0.70    # 업종 상위 30% = 70백분위 이상
G_GPM_DECLINE_MAX = 0.02          # 1년 전보다 2%p 넘게 하락 시 탈락
# 매출원가를 공시하지 않아 매출총이익률이 없는 기업: 매출총이익률 조건을 건너뛰고
# 대신 영업이익률이 1년 전보다 악화되지 않았는지로 판정 (2026-10-19)
G_NO_GPM_OPM_CHANGE_MIN = 0.0

# H4 C 차선 (느린 경로)
C_REV_GROWTH_MIN = 0.10
C_FCF_YIELD_PCTL = 0.70           # 유니버스 상위 30%
C_GPM_FLOOR = 0.20
C_MKT_CAP_MAX = 10e9              # 2026-10-19: C 차선만 시총 상한 100억 달러 (성숙 대형주 제외)
# 2026-10-19: 경기 민감 원자재 업종은 C 차선에서 제외 (사이클 정점의 높은 FCF 수익률 착시)
# SIC 1000~1499 광업·석탄·석유가스 채굴·시추, 2900~2999 석유 정제, 2870~2879 비료·농화학,
# 4400~4499 해운(운임 사이클)
C_EXCLUDED_SIC_RANGES = [(1000, 1499), (2900, 2999), (2870, 2879), (4400, 4499)]
# G 차선에서는 제외하지 않고 메일에 '경기 민감 업종' 표시만 한다

# 기저 효과 방지: 1년 전 TTM 매출이 이보다 작으면 성장률을 믿지 않는다 (2026-10-19)
PRIOR_REVENUE_MIN = 25e6

# 업종 비교: SIC 3자리 → 표본 부족 시 2자리 → 전체
INDUSTRY_MIN_PEERS = 8

# ---------- 2층: 자동 계산 항목 (69점 만점) ----------
# 수동 항목(S4 잔고 5, S11 역DCF 10, S14 TAM 4, S15 경영진 4, S16 내부자 3,
# S17 가이던스 3, S18 기관 보유 2 = 31점)은 체크리스트 검증 단계에서 채운다.
AUTO_SCORE_MAX = 69
PRIORITY_SCORE_PCT = 0.70         # 자동 점수 70% 이상 = 1순위 후보

# ---------- 메일 ----------
TOP_N = 30
MAX_ROWS_PER_SECTION = 40

# ---------- 데이터 갱신 ----------
FACTS_MAX_AGE_DAYS = 45           # 이보다 오래된 재무 캐시는 순차 재수집
STALE_REFRESH_PER_RUN = 600       # 한 번 실행에 재수집할 오래된 캐시 최대 수
INDEX_LOOKBACK_DAYS = 3           # 매 실행마다 최근 N일 EDGAR 공시 목록을 다시 확인
FILING_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT"}
