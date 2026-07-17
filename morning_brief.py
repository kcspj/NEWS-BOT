import os, json, base64, time
import feedparser
import requests
import holidays
from datetime import datetime, timezone, timedelta, date as date_cls
from icalendar import Calendar
import recurring_ical_events

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

# 구글 캘린더 (비공개 주소 - Secret address in iCal format 방식)
# 캘린더 설정 → 캘린더 통합 → "비공개 주소(iCal 형식)" 에서 발급되는 .ics URL
# 여러 캘린더를 합치려면 콤마(,) 또는 줄바꿈으로 구분해서 저장
GCAL_ICS_URLS = os.environ.get("GCAL_ICS_URLS", "")

# 뉴스 소스
NEWS_FEEDS = [
    "https://rss.hankyung.com/economy.xml",
    "https://www.mk.co.kr/rss/30000001/",
    "https://news.google.com/rss/search?q=%EB%AF%B8%EA%B5%AD+%EC%A6%9D%EC%8B%9C&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=%EC%97%B0%EC%A4%80+%EA%B8%88%EB%A6%AC+%EA%B2%BD%EC%A0%9C&hl=ko&gl=KR&ceid=KR:ko",
]

# 시장 지표
TICKERS = {
    "나스닥100":   "^NDX",
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


def fetch_ticker(symbol: str) -> tuple[float, float, str] | None:
    """Yahoo Finance v8 API로 종가 2일치 직접 조회
    - range=10d로 충분한 데이터 확보
    - None(미확정) 제거 후 가장 최근 확정 종가 2개 사용
    - timestamp를 이용해 '가장 최근 확정 종가가 실제로 몇 월 며칠자 마감인지'를 함께 반환
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=10d"
    )
    try:
        r = requests.get(url, headers=YF_HEADERS, timeout=15)
        if r.status_code != 200:
            url2 = url.replace("query1", "query2")
            r = requests.get(url2, headers=YF_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        result = r.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        timestamps = result.get("timestamp", [])
        # None(당일 미확정) 제거 후 (timestamp, close) 쌍으로 최근 확정 2개 사용
        confirmed = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        if len(confirmed) < 2:
            return None
        (ts_last, c_last), (_, c_prev) = confirmed[-1], confirmed[-2]
        # 미국 거래소 종가 timestamp를 한국시간(KST) 날짜로 변환
        # (예: 미국 동부시간 7/16 16:00 마감 → KST로는 7/17 새벽 → "07월 17일"로 정확히 표시)
        last_date = datetime.fromtimestamp(ts_last, tz=KST).strftime("%m월 %d일")
        print(f"    {symbol}: 최근확정({last_date})={c_last:.2f} 전일={c_prev:.2f}")
        return c_last, c_prev, last_date
    except Exception as e:
        print(f"    fetch_ticker({symbol}) 오류: {e}")
        return None


def get_last_kr_trading_date(now: datetime) -> str:
    """가장 최근에 '완료된' 국내 거래일을 계산 (주말·공휴일 제외)
    - 브리핑은 항상 국내 개장(09:00) 전에 실행되므로, 오늘은 아직 거래일이 아님
    - 어제부터 거슬러 올라가며 주말/공휴일이 아닌 첫 날을 찾는다
    """
    kr_holidays = holidays.KR(years=[now.year - 1, now.year])
    d = now.date() - timedelta(days=1)
    while d.weekday() >= 5 or d in kr_holidays:
        d -= timedelta(days=1)
    return d.strftime("%m월 %d일")


def fetch_naver_index(symbol: str) -> tuple[float, float, str] | None:
    """네이버 API로 한국 지수 현재가 및 등락 가져오기
    - 국내 지수의 '기준 날짜'는 네이버 API가 아니라 거래일 계산 로직으로 직접 산출
      (장전에는 change/pct가 0으로 오는 경우가 있어, 그 값을 신뢰하지 않고
       history 유무와 무관하게 날짜만 별도로 계산)
    """
    naver_map = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ", "^KS200": "KPI200"}
    naver_sym = naver_map.get(symbol)
    if not naver_sym:
        return None
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    try:
        d = requests.get(
            f"https://m.stock.naver.com/api/index/{naver_sym}/basic",
            headers=headers, timeout=5
        ).json()
        current = float(d["closePrice"].replace(",", ""))
        change  = float(d["compareToPreviousClosePrice"].replace(",", ""))
        prev    = current - change
        kr_date = get_last_kr_trading_date(datetime.now(KST))
        return current, prev, kr_date
    except Exception as e:
        print(f"    네이버 {symbol} 오류: {e}")
        return None


def get_market_data() -> tuple[str, dict, str, str]:
    """시장 지표 수집 — (표시용 텍스트, 허브용 dict, 미국시장 기준일, 국내시장 기준일) 반환"""
    lines    = []
    market   = {}
    us_date  = None
    kr_date  = None
    KR_SYMBOLS = {"^KS11", "^KQ11", "^KS200"}
    for name, symbol in TICKERS.items():
        is_kr  = symbol in KR_SYMBOLS
        result = fetch_naver_index(symbol) if is_kr else fetch_ticker(symbol)
        if result:
            curr, prev, date_str = result
            pct   = (curr - prev) / prev * 100
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "━")
            lines.append(f"  [{date_str} 마감] {name:<12}: {curr:>10,.2f}  {arrow}{abs(pct):.2f}%")
            market[name] = f"{curr:,.2f} {arrow}{abs(pct):.2f}%"
            # 대표 기준일 저장 (코스피 → 국내 기준일 / 나스닥100 → 미국 기준일)
            if is_kr and kr_date is None:
                kr_date = date_str
            if not is_kr and us_date is None:
                us_date = date_str
        else:
            print(f"  ⚠️ {name}({symbol}) 수집 실패")
            lines.append(f"  {name:<12}: 데이터 없음")
            market[name] = "데이터 없음"
        time.sleep(0.3)  # 요청 간 짧은 딜레이
    return "\n".join(lines), market, us_date, kr_date


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


def get_today_schedule() -> list[dict]:
    """구글 캘린더 '비공개 주소(iCal)'에서 오늘 하루 일정 조회
    - GCAL_ICS_URLS: .ics URL을 콤마 또는 줄바꿈으로 구분해 여러 개 지정 가능
    - 반복 일정(RRULE)도 recurring_ical_events로 오늘자에 맞게 전개하여 처리
    - 반환 형식: [{"time": "종일" 또는 "04:00", "name": "일정 제목"}, ...]
    """
    raw_urls = GCAL_ICS_URLS.replace("\n", ",")
    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    if not urls:
        print("  ⏭️ 캘린더 조회 스킵 (GCAL_ICS_URLS 미설정)")
        return []

    now   = datetime.now(KST)
    today = now.date()
    schedule = []

    for url in urls:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                print(f"  ⚠️ ICS 다운로드 실패 ({r.status_code}): {url[:60]}...")
                continue
            cal = Calendar.from_ical(r.content)
            # 오늘 하루(00:00~24:00, KST) 범위로 반복 일정까지 전개
            events = recurring_ical_events.of(cal).between(
                (today.year, today.month, today.day, 0, 0, 0),
                (today.year, today.month, today.day, 23, 59, 59),
            )
            for ev in events:
                name  = str(ev.get("summary", "(제목 없음)"))
                dtstart = ev["DTSTART"].dt
                if isinstance(dtstart, date_cls) and not isinstance(dtstart, datetime):
                    # 종일 일정 (date만 있고 시간 없음)
                    schedule.append({"time": "종일", "name": name})
                else:
                    t = dtstart.astimezone(KST) if dtstart.tzinfo else dtstart.replace(tzinfo=KST)
                    schedule.append({"time": t.strftime("%H:%M"), "name": name})
        except Exception as e:
            print(f"  ⚠️ ICS 파싱 오류 ({url[:60]}...): {e}")
            continue

    schedule.sort(key=lambda s: ("0" if s["time"] == "종일" else "1" + s["time"]))
    print(f"  ✅ 오늘 일정 {len(schedule)}건 조회 완료")
    return schedule


def format_schedule_text(schedule: list[dict]) -> str:
    """텔레그램 메시지용 일정 텍스트 생성"""
    if not schedule:
        return ""
    lines = [f"  ·{s['time']}  {s['name']}" for s in schedule]
    return "📅 오늘의 일정\n" + "\n".join(lines) + "\n\n"


def get_ai_report(market_data: str, news_titles: list[str],
                   us_date: str | None, kr_date: str | None) -> str:
    """Gemini 2.5 Flash로 AI 브리핑 생성"""
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    _now_kst   = datetime.now(KST)
    today_str  = _now_kst.strftime("%Y년 %m월 %d일")
    day_kr     = weekday_kr[_now_kst.weekday()]
    us_date_str = us_date or "확인불가"
    kr_date_str = kr_date or "확인불가"

    prompt = f"""당신은 한국의 증권사 리서치센터 수석 애널리스트입니다.

오늘 브리핑 발행일은 {today_str} ({day_kr}요일)입니다.

[중요 — 날짜 표기 규칙, 반드시 지킬 것]
- "전날", "어제", "오늘", "다음날" 등 상대적인 날짜 표현을 절대 쓰지 마세요.
- 미국 시장 데이터는 반드시 "{us_date_str} 마감 미국 증시"처럼 정확한 날짜를 명시하세요.
- 국내 시장 데이터는 반드시 "{kr_date_str} 마감 국내 증시"처럼 정확한 날짜를 명시하세요.
- 미국 시장과 국내 시장의 기준일이 서로 다를 수 있습니다({us_date_str} vs {kr_date_str}).
  두 시장을 같은 날짜의 사건인 것처럼 섞어 쓰지 말고, 각 데이터가 어느 날짜 마감 기준인지
  문장마다 명확히 구분해서 서술하세요.

[시장 지표] (각 줄 맨 앞 [ ] 안이 해당 지표의 실제 마감일입니다)
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
    market_text, market_dict, us_date, kr_date = get_market_data()
    print(market_text)
    print(f"  ▶ 미국 시장 기준일: {us_date} / 국내 시장 기준일: {kr_date}")

    print("\n📅 오늘 일정 조회 중...")
    schedule = get_today_schedule()

    print("\n📰 뉴스 수집 중...")
    titles, links_text, links_json = get_news()
    for t in titles:
        print(f"  - {t}")

    print("\n🤖 AI 브리핑 생성 중...")
    ai_summary = get_ai_report(market_text, titles, us_date, kr_date)

    # 텔레그램 발송
    separator = "─" * 30
    full_msg  = (
        f"{today} {title}\n\n"
        f"{format_schedule_text(schedule)}"
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
        "date":     today,
        "title":    title,
        "content":  ai_summary,
        "market":   market_dict,
        "links":    links_json,
        "schedule": schedule,
    })

    print("\n✅ 전체 완료!")


if __name__ == "__main__":
    main()
