"""기간 리뷰 — history/*.json(일별 기록)을 모아 주간/월간 분석을 만들어 텔레그램으로 발송

사용:
    REVIEW_DAYS=30 python period_review.py    # 월간 (기본값 30)
    REVIEW_DAYS=7  python period_review.py    # 주간

원칙: 숫자(변화율·고저점·등락일수)는 코드가 계산하고, AI는 해석만 맡긴다.
"""
import os, json, base64
import requests
from datetime import datetime, timedelta

# morning_brief 와 같은 환경변수(Secrets)를 그대로 사용
from morning_brief import (
    KST, GITHUB_TOKEN, GITHUB_USER, GITHUB_REPO,
    call_gemini, send_telegram,
)

REVIEW_DAYS = int(os.environ.get("REVIEW_DAYS", "30"))
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
API = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents"


def load_history(days: int) -> list[dict]:
    """history/ 폴더에서 최근 N일치 일별 기록을 읽어 날짜순으로 반환"""
    r = requests.get(f"{API}/history", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"  ❌ history 폴더 조회 실패 ({r.status_code}): {r.text[:200]}")
        return []

    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    names = sorted(
        f["name"] for f in r.json()
        if f["name"].endswith(".json") and f["name"][:-5] >= cutoff
    )

    records = []
    for name in names:
        try:
            fr = requests.get(f"{API}/history/{name}", headers=HEADERS, timeout=15)
            if fr.status_code != 200:
                print(f"  ⚠️ {name} 읽기 실패 ({fr.status_code})")
                continue
            records.append(json.loads(base64.b64decode(fr.json()["content"]).decode("utf-8")))
        except Exception as e:
            print(f"  ⚠️ {name} 파싱 오류: {e}")
    return records


def compute_stats(records: list[dict]) -> str:
    """지표별 기간 통계를 코드로 계산해 표 형태 텍스트로 반환"""
    series: dict[str, list[tuple[str, float, float]]] = {}
    for rec in records:
        for name, v in (rec.get("market_raw") or {}).items():
            series.setdefault(name, []).append((rec["date"], v["close"], v["pct"]))

    lines = []
    for name, rows in series.items():
        if len(rows) < 2:
            continue
        first_d, first_c, _ = rows[0]
        last_d, last_c, _ = rows[-1]
        hi = max(rows, key=lambda x: x[1])
        lo = min(rows, key=lambda x: x[1])
        big = max(rows, key=lambda x: abs(x[2]))  # 일간 변동폭이 가장 컸던 날
        up = sum(1 for _, _, p in rows if p > 0)
        dn = sum(1 for _, _, p in rows if p < 0)
        chg = (last_c - first_c) / first_c * 100
        lines.append(
            f"- {name}: {first_c:,.2f}({first_d}) → {last_c:,.2f}({last_d}) {chg:+.2f}% | "
            f"고점 {hi[1]:,.2f}({hi[0]}) 저점 {lo[1]:,.2f}({lo[0]}) | "
            f"상승 {up}일/하락 {dn}일 | 최대 일간변동 {big[2]:+.2f}%({big[0]})"
        )
    return "\n".join(lines)


def build_prompt(records: list[dict], stats: str) -> str:
    start, end = records[0]["date"], records[-1]["date"]
    # AI 호출 실패 메시지(⚠️)가 저장된 날은 리포트 본문에서 제외
    daily = "\n\n".join(
        f"[{r['date']}]\n{r['content']}" for r in records
        if r.get("content") and not r["content"].startswith("⚠️")
    )
    return f"""당신은 한국의 증권사 리서치센터 수석 애널리스트입니다.
아래는 {start} ~ {end} 기간({len(records)}회 발행)의 일별 시장 데이터 통계와 일간 AI 브리핑 원문입니다.

[작성 규칙]
- "어제", "지난주" 같은 상대적 날짜 표현 금지. 반드시 YYYY-MM-DD 날짜를 명시하세요.
- 숫자는 아래 [기간 통계]에 있는 값만 사용하고, 직접 계산하거나 새 수치를 만들지 마세요.
- 앞으로의 주가를 단정적으로 예측하지 말고, 확인할 지표와 조건만 제시하세요.
- 일간 브리핑이 서로 모순되거나 한쪽으로 쏠렸다면 그 점도 지적하세요.
- 800자 내외, 아래 형식을 지키세요.

[기간 통계 — 코드로 계산된 값]
{stats}

[일간 AI 브리핑 원문]
{daily}

■ 기간 요약
■ 지표별 흐름 (주식·금리·환율·원자재)
■ 반복된 이슈와 변곡점 (날짜 포함)
■ 일간 브리핑의 일관성 점검
■ 다음 기간 점검 포인트 (예측 아님, 확인할 지표/조건)"""


def main():
    if not GITHUB_TOKEN or not GITHUB_USER:
        print("GH_PAT / GH_USER 가 필요합니다.")
        return

    print(f"\n📚 최근 {REVIEW_DAYS}일 기록 불러오는 중...")
    records = load_history(REVIEW_DAYS)
    print(f"  → {len(records)}개 일자 로드")
    if len(records) < 3:
        print("  ⏭️ 기록이 3일 미만이라 리뷰를 건너뜁니다.")
        return

    stats = compute_stats(records)
    print(stats)

    print("\n🤖 기간 리뷰 생성 중...")
    review = call_gemini(build_prompt(records, stats))

    label = "월간" if REVIEW_DAYS >= 28 else ("주간" if REVIEW_DAYS <= 8 else f"{REVIEW_DAYS}일")
    msg = (
        f"📊 {label} AI 리뷰 ({records[0]['date']} ~ {records[-1]['date']}, {len(records)}회)\n\n"
        f"[기간 통계]\n{stats}\n\n{review}"
    )
    send_telegram(msg)
    print("\n✅ 완료!")


if __name__ == "__main__":
    main()
