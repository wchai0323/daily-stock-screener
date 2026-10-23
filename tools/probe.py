"""외부 데이터 접속 점검 (시황 브리핑용). 결과를 data/probe.json에 남긴다."""
import json, os, time, traceback
import requests

out = {}
def rec(name, fn):
    t = time.time()
    try:
        out[name] = {"ok": True, "detail": fn()}
    except Exception as e:  # noqa: BLE001
        out[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    out[name]["sec"] = round(time.time() - t, 1)

UA = {"User-Agent": "Mozilla/5.0"}
tok = os.environ.get("GITHUB_TOKEN", "")

def models_catalog():
    r = requests.get("https://models.github.ai/catalog/models", headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    return [m.get("id") for m in r.json()][:80]

def models_chat(model):
    def f():
        r = requests.post("https://models.github.ai/inference/chat/completions",
                          headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                          json={"model": model, "messages": [
                              {"role": "user", "content": "반도체 장비사의 공급망 제약 경고가 왜 주가에 악재인지 초보자에게 두 문장으로 설명해줘."}],
                              "max_tokens": 300}, timeout=60)
        return {"status": r.status_code, "headers": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()},
                "body": r.text[:900]}
    return f

def fred():
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10,BAMLH0A0HYM2", headers=UA, timeout=30)
    r.raise_for_status(); lines = r.text.strip().splitlines(); return [lines[0], lines[-1], len(lines)]

def gnews():
    r = requests.get("https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US&gl=US&ceid=US:en", headers=UA, timeout=30)
    r.raise_for_status(); return [r.text.count("<item>"), r.text[:200]]

def gnews_ko():
    r = requests.get("https://news.google.com/rss/search?q=%EC%BD%94%EC%8A%A4%ED%94%BC+when:1d&hl=ko&gl=KR&ceid=KR:ko", headers=UA, timeout=30)
    r.raise_for_status(); return r.text.count("<item>")

def yrss():
    r = requests.get("https://feeds.finance.yahoo.com/rss/2.0/headline?s=KLIC&region=US&lang=en-US", headers=UA, timeout=30)
    r.raise_for_status(); return r.text.count("<item>")

def yindex():
    import yfinance as yf
    df = yf.download(["^GSPC", "^IXIC", "^VIX", "^VIX3M", "SOXX", "IWO", "IWD", "KRW=X", "CL=F", "BTC-USD"], period="5d", progress=False, group_by="ticker")
    return {t: (float(df[t]["Close"].dropna().iloc[-1]) if not df[t]["Close"].dropna().empty else None) for t in ["^GSPC", "^VIX", "^VIX3M", "SOXX", "KRW=X", "CL=F", "BTC-USD"]}

def naver_index():
    r = requests.get("https://api.finance.naver.com/siseJson.naver", params={"symbol": "KOSPI", "requestType": 1, "startTime": "20261015", "endTime": "20261023", "timeframe": "day"}, headers=UA, timeout=30)
    return r.text[:300]

def naver_flow():
    r = requests.get("https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate=20261022&sosok=", headers=UA, timeout=30)
    return [r.status_code, len(r.text)]

rec("github_models_catalog", models_catalog)
for m in ["openai/gpt-4.1-mini", "openai/gpt-4o-mini", "openai/gpt-5-mini", "deepseek/deepseek-v3-0324"]:
    rec("chat:" + m, models_chat(m))
rec("fred", fred); rec("google_news", gnews); rec("google_news_ko", gnews_ko); rec("yahoo_rss", yrss)
rec("yahoo_prices", yindex); rec("naver_kospi", naver_index); rec("naver_flow", naver_flow)
os.makedirs("data", exist_ok=True)
json.dump(out, open("data/probe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
