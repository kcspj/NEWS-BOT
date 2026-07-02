import os, json, base64, time
import feedparser
import requests
import holidays
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────
# [설정 정보] — GitHub Actions Secrets에서 자동 주입
# ─────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["CHAT_ID"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN    = os.environ.get("GH_PAT", "")
GITHUB_USER     = os.environ.get("GH_USER", "")
GITHUB_REPO     = os.environ.get("GH_REPO", "NEWS-BOT")

# 뉴스 소스
NEWS_FEEDS = [
    "https://rss.hankyung.com/economy.xml",
    "https://www.mk.co.kr/rss/30000001/",
    "https://news.google.com/rss/search?q=%EB%AF%B8%EA%B5%AD+%EC%A6%9D%EC%8B%9C&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=%EC%97%B0%EC%A4%80+%EA%B8%88%EB%A6%AC+%EA%B2%BD%EC%A0%9C&hl=ko&gl=KR&ceid=KR:ko",
]

# 시장 지표
TICKERS = {
    "나스닥100":     "^NDX",
    "S&P500":    "^GSPC",
    "다우존스":   "^DJI",
    "코스피":     "^KS11",
    "코스닥":     "^KQ11",
    "원/달러":    "KRW=X",
    "달러인덱스": "DX-Y.NYB",
    "미국채10년": "^TNX",
    "VIX(공포)":  "^VIX",
    "금":         "GC=F",
    "WTI유가":    "CL=F",
}

# Yahoo Finance v8 API 헤더 (GitHub Actions 환경 우회)
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}
# ─────────────────────────────────────────


def fetch_ticker(symbol: str) -> tuple[float, float] | None:
    """Yahoo Finance v8 API로 종가 2일치 직접 조회 (당일 미확정 데이터 제외)"""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=5d"
    )
    # 한국 지수 여부 판단 (KST 기준 필터링 필요)
    KR_SYMBOLS = {"^KS11", "^KQ11", "^KS200"}
    is_kr = symbol in KR_SYMBOLS
    from datetime import timezone, timedelta
    KST = timezone(timedelta(hours=9))
    UTC = timezone.utc
    # 기준: 한국 지수는 KST, 해외 지수는 UTC 기준 전날까지만 사용
    ref_tz   = KST if is_kr else UTC
    ref_date = datetime.now(ref_tz).date()
    try:
        r = requests.get(url, headers=YF_HEADERS, timeout=15)
        if r.status_code != 200:
            url2 = url.replace("query1", "query2")
            r = requests.get(url2, headers=YF_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        result     = r.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes     = result["indicators"]["quote"][0]["close"]
        # 당일(기준시간대) 미확정 데이터 제외
        filtered = [
            c for ts, c in zip(timestamps, closes)
            if c is not None and datetime.fromtimestamp(ts, tz=ref_tz).date() < ref_date
        ]
        if len(filtered) < 2:
            return None
        return filtered[-1], filtered[-2]
    except Exception as e:
        print(f"    fetch_ticker({symbol}) 오류: {e}")
        return None


def get_market_data() -> tuple[str, dict]:
    """시장 지표 수집 — (표시용 텍스트, 허브용 dict) 반환"""
    lines  = []
    market = {}
    for name, symbol in TICKERS.items():
        result = fetch_ticker(symbol)
        if result:
            curr, prev = result
            pct   = (curr - prev) / prev * 100
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "━")
            lines.append(f"  {name:<12}: {curr:>10,.2f}  {arrow}{abs(pct):.2f}%")
            market[name] = f"{curr:,.2f} {arrow}{abs(pct):.2f}%"
        else:
            print(f"  ⚠️ {name}({symbol}) 수집 실패")
            lines.append(f"  {name:<12}: 데이터 없음")
            market[name] = "데이터 없음"
        time.sleep(0.3)  # 요청 간 짧은 딜레이
    return "\n".join(lines), market


def get_news() -> tuple[list[str], str, list[dict]]:
    """복수 RSS 피드에서 뉴스 수집"""
    entries = []
    seen    = set()
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:2]:
                title = e.get("title", "").strip()
                if title and title not in seen:
                    seen.add(title)
                    entries.append(e)
        except Exception as ex:
            print(f"  ⚠️ RSS 수집 실패 ({url[:50]}…): {ex}")

    entries = entries[:6]
    titles  = [e.title for e in entries]
    links_text = "".join(f"• {e.title}\n  🔗 {e.link}\n\n" for e in entries)
    links_json = [{"title": e.title, "url": e.link} for e in entries]
    return titles, links_text, links_json


def get_ai_report(market_data: str, news_titles: list[str]) -> str:
    """Gemini 2.5 Flash로 AI 브리핑 생성"""
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    _now_kst   = datetime.now(KST)
    today_str  = _now_kst.strftime("%Y년 %m월 %d일")
    day_kr     = weekday_kr[_now_kst.weekday()]

    prompt = f"""당신은 한국의 증권사 리서치센터 수석 애널리스트입니다.

오늘은 {today_str} ({day_kr}요일)이며, 전날 밤 마감된 미국 시장 기준으로 분석하세요.

[시장 지표]
{market_data}

[주요 뉴스 헤드라인]
{chr(10).join(f"- {t}" for t in news_titles)}

아래 형식으로 500자 내외로 간결하게 작성하세요.
숫자·등락폭을 근거로 제시하고, 막연한 전망은 피하세요.

■ 시장 요약
■ 핵심 이슈 3가지
■ 미국 증시 영향
■ 한국 증시 영향
■ 관심 업종
■ 투자 포인트"""

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = requests.post(api_url,
                             json={"contents": [{"parts": [{"text": prompt}]}]},
                             headers={"Content-Type": "application/json"},
                             timeout=40)
        print(f"  Gemini 응답 코드: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  Gemini 오류: {resp.text[:300]}")
            return "⚠️ AI 분석을 가져오지 못했습니다."
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  Gemini 통신 오류: {e}")
        return f"⚠️ Gemini 통신 오류: {e}"


def send_telegram(text: str) -> None:
    """텔레그램 발송 (4000자 초과 시 자동 분할)"""
    MAX   = 4000
    url   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i, chunk in enumerate([text[i:i+MAX] for i in range(0, len(text), MAX)]):
        try:
            r = requests.post(url, json={
                "chat_id": CHAT_ID, "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=15)
            status = "✅" if r.status_code == 200 else f"❌({r.status_code})"
            print(f"  텔레그램 {i+1}번째 전송 {status}")
        except Exception as e:
            print(f"  텔레그램 전송 실패: {e}")


# ✅ 추가: GitHub에 briefing.json 저장
def save_to_github(payload: dict) -> None:
    """briefing.json 을 GitHub 저장소에 저장 (허브 뉴스룸용)"""
    if not GITHUB_TOKEN or not GITHUB_USER:
        print("  ⏭️ GitHub 저장 스킵 (GITHUB_TOKEN / GITHUB_USER 미설정)")
        return

    api_url  = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/briefing.json"
    headers  = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    content  = base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode()

    # 기존 파일 SHA 조회 (업데이트 시 필요)
    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    body = {
        "message": f"briefing: {payload['date']}",
        "content": content,
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(api_url, json=body, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            print("  ✅ GitHub briefing.json 저장 완료!")
        else:
            print(f"  ❌ GitHub 저장 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"  ❌ GitHub 저장 오류: {e}")


def main():
    now        = datetime.now(KST)
    today      = now.strftime("%Y-%m-%d")
    is_weekend = now.weekday() >= 5

    # 한국 공휴일 체크
    kr_holidays = holidays.KR(years=now.year)
    if now.date() in kr_holidays:
        holiday_name = kr_holidays.get(now.date())
        print(f"  오늘은 한국 공휴일({holiday_name}) — 브리핑 스킵")
        return
    title      = "☀️ 주말 AI 경제 브리핑" if is_weekend else "☀️ AI 경제 브리핑"

    print(f"\n{'='*50}")
    print(f"  {today} {title}")
    print(f"{'='*50}")

    print("\n📊 시장 지표 수집 중...")
    market_text, market_dict = get_market_data()
    print(market_text)

    print("\n📰 뉴스 수집 중...")
    titles, links_text, links_json = get_news()
    for t in titles:
        print(f"  - {t}")

    print("\n🤖 AI 브리핑 생성 중...")
    ai_summary = get_ai_report(market_text, titles)

    # 텔레그램 발송
    separator = "─" * 30
    full_msg  = (
        f"{today} {title}\n\n"
        f"{ai_summary}\n\n"
        f"{separator}\n"
        f"🔗 뉴스 원문\n\n"
        f"{links_text}"
    )
    print("\n📨 텔레그램 발송 중...")
    send_telegram(full_msg)

    # ✅ GitHub 저장 (허브 뉴스룸 연동)
    print("\n💾 GitHub 저장 중...")
    save_to_github({
        "date":    today,
        "title":   title,
        "content": ai_summary,
        "market":  market_dict,
        "links":   links_json,
    })

    print("\n✅ 전체 완료!")


if __name__ == "__main__":
    main()
