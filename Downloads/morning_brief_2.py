import os
import yfinance as yf
import feedparser
import requests
from datetime import datetime

# ─────────────────────────────────────────
# [설정 정보] — 여기만 수정하세요
# ─────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# 뉴스 소스 (각 소스에서 최대 2건씩)
NEWS_FEEDS = [
    # 국내 경제
    "https://rss.hankyung.com/economy.xml",
    "https://www.mk.co.kr/rss/30000001/",
    # 미국 증시·경제 (구글뉴스 한국어)
    "https://news.google.com/rss/search?q=%EB%AF%B8%EA%B5%AD+%EC%A6%9D%EC%8B%9C&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=%EC%97%B0%EC%A4%80+%EA%B8%88%EB%A6%AC+%EA%B2%BD%EC%A0%9C&hl=ko&gl=KR&ceid=KR:ko",
]

# 시장 지표
TICKERS = {
    "나스닥":       "^IXIC",
    "S&P500":      "^GSPC",
    "다우존스":     "^DJI",
    "코스피":       "^KS11",
    "코스닥":       "^KQ11",
    "원/달러":      "KRW=X",
    "달러인덱스":   "DX-Y.NYB",
    "미국채10년":   "^TNX",
    "VIX(공포)":    "^VIX",
    "금":           "GC=F",
    "WTI유가":      "CL=F",
}
# ─────────────────────────────────────────


def get_market_data() -> str:
    """시장 지표 수집 — 수집 실패 항목은 '데이터 없음'으로 표시"""
    lines = []
    for name, symbol in TICKERS.items():
        try:
            data = yf.Ticker(symbol).history(period="2d")
            if len(data) < 2:
                raise ValueError("데이터 부족")
            curr = data["Close"].iloc[-1]
            prev = data["Close"].iloc[-2]
            pct  = (curr - prev) / prev * 100
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "━")
            lines.append(f"  {name:<12}: {curr:>10,.2f}  {arrow}{abs(pct):.2f}%")
        except Exception as e:
            print(f"  ⚠️ {name}({symbol}) 수집 실패: {e}")
            lines.append(f"  {name:<12}: 데이터 없음")
    return "\n".join(lines)


def get_news() -> tuple[list[str], str]:
    """복수 RSS 피드에서 뉴스 수집 — 중복 제목 제거 후 최대 6건"""
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
    links   = "".join(
        f"• {e.title}\n  🔗 {e.link}\n\n" for e in entries
    )
    return titles, links


def get_ai_report(market_data: str, news_titles: list[str]) -> str:
    """Gemini 2.5 Flash로 AI 브리핑 생성"""
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    today_str  = datetime.now().strftime("%Y년 %m월 %d일")
    day_kr     = weekday_kr[datetime.now().weekday()]

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
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = requests.post(api_url, json=payload,
                             headers={"Content-Type": "application/json"},
                             timeout=40)
        print(f"  Gemini 응답 코드: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  Gemini 오류 내용: {resp.text[:300]}")
            return "⚠️ AI 분석을 가져오지 못했습니다."

        return (
            resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            .strip()
        )
    except Exception as e:
        print(f"  Gemini 통신 오류: {e}")
        return f"⚠️ Gemini 통신 오류: {e}"


def send_telegram(text: str) -> None:
    """텔레그램 메시지 발송 (4096자 초과 시 자동 분할)"""
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(url, json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=15)
            status = "✅" if r.status_code == 200 else f"❌({r.status_code})"
            print(f"  텔레그램 {i+1}/{len(chunks)}번째 전송 {status}")
        except Exception as e:
            print(f"  텔레그램 전송 실패: {e}")


def main():
    now      = datetime.now()
    today    = now.strftime("%Y-%m-%d")
    is_weekend = now.weekday() >= 5
    title    = "☀️ 주말 AI 경제 브리핑" if is_weekend else "☀️ AI 경제 브리핑"

    print(f"\n{'='*50}")
    print(f"  {today} {title}")
    print(f"{'='*50}")

    print("\n📊 시장 지표 수집 중...")
    market_info = get_market_data()
    print(market_info)

    print("\n📰 뉴스 수집 중...")
    titles, links = get_news()
    for t in titles:
        print(f"  - {t}")

    print("\n🤖 AI 브리핑 생성 중...")
    ai_summary = get_ai_report(market_info, titles)

    separator = "─" * 30
    full_msg = (
        f"{today} {title}\n\n"
        f"{ai_summary}\n\n"
        f"{separator}\n"
        f"🔗 뉴스 원문\n\n"
        f"{links}"
    )

    print("\n📨 텔레그램 발송 중...")
    send_telegram(full_msg)
    print("\n✅ 완료!")


if __name__ == "__main__":
    main()
