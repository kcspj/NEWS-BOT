import yfinance as yf
import feedparser
import requests
import json
from datetime import datetime

# --- [설정 정보] ---
TELEGRAM_TOKEN = '8865628224:AAH3Lz24HpM1s9Vw--WpGlgBcrlb3sByjnE'
CHAT_ID = "8931910080"
# 다른 서버에서 쓰시던 그 AQ... 시작 키를 그대로 넣으세요
GEMINI_API_KEY = 'AQ.Ab8RN6JJNmzhw2Be-TTe1iC6P6J9fyPsONr0WiZhDmoqV5Vcug'

def get_market_data():
    tickers = {"나스닥": "^IXIC", "S&P500": "^GSPC", "코스피": "^KS11", "원/달러": "KRW=X"}
    res = ""
    for name, ticker in tickers.items():
        try:
            data = yf.Ticker(ticker).history(period="2d")
            curr, prev = data['Close'].iloc[-1], data['Close'].iloc[-2]
            pct = ((curr - prev) / prev) * 100
            sign = "▲" if pct > 0 else "▼"
            res += f"{name}: {curr:,.2f} ({sign}{pct:.2f}%)\n"
        except: pass
    return res

def get_news():
    url = "https://news.google.com/rss/search?q=%EB%AF%B8%EA%B5%AD%20%EC%A6%9D%EC%8B%9C%20%EA%B2%BD%EC%A0%9C%20%EC%A7%80%ED%91%9C&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url)
    titles = [e.title for e in feed.entries[:3]]
    links = "".join([f"• {e.title}\n  🔗 {e.link}\n\n" for e in feed.entries[:3]])
    return titles, links

def get_ai_report(market_data, news_titles):

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

    headers = {
        "Content-Type": "application/json"
    }

    prompt = f"""
당신은 한국의 증권사 리서치센터 수석 애널리스트입니다.

시장지표:
{market_data}

뉴스:
{news_titles}

아래 형식으로 작성하세요.

■ 시장 요약

■ 오늘의 핵심 이슈 3가지

■ 미국 증시 영향

■ 한국 증시 영향

■ 관심 업종

■ 투자 포인트

500자 내외로 작성
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }

    try:

        response = requests.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=30
        )

        print("상태코드 :", response.status_code)

        if response.status_code != 200:
            print(response.text)
            return "⚠️ AI 서버 인증 지연이 발생했습니다."

        res_data = response.json()

        full_text = (
            res_data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return full_text.strip()

    except Exception as e:

        print("Gemini 오류 :", e)

        return f"⚠️ Gemini 통신 오류 : {e}"
def main():
    print("🚀 최신 보안 방식(헤더 전송)으로 보고서 생성 중...")
    # 토요일(5), 일요일(6)
    weekday = datetime.now().weekday()

    if weekday >= 5:
        print("주말 브리핑 모드")

    market_info = get_market_data()

    titles, links = get_news()

    ai_summary = get_ai_report(
        market_info,
        titles
    )
    today = datetime.now().strftime('%Y-%m-%d')

    if datetime.now().weekday() >= 5:
        title = "☀️ 주말 AI 경제 브리핑"
    else:
        title = "☀️ AI 경제 브리핑"

    full_msg = f"{today} {title}\n\n{ai_summary}\n\n---\n🔗 뉴스 링크\n{links}"
   
    # 텔레그램 최대 길이 보호
    if len(full_msg) > 3900:
       full_msg = full_msg[:3900]

    requests.post(
         f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
         json={"chat_id": CHAT_ID, "text": full_msg, "disable_web_page_preview": True})
    print("✅ 완료! 텔레그램을 확인하세요.")

if __name__ == "__main__":
    main()