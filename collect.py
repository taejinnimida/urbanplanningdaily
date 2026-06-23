from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE_PATH = DATA / "archive.json"
LATEST_PATH = DATA / "latest.json"
KEYWORDS_PATH = DATA / "keywords.json"
ISSUES_PATH = DATA / "issues.json"
BACKFILL_PATH = DATA / "backfill_complete.json"

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
TIMEOUT = 25

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UrbanPlanningDaily/2.0; +https://taejinnimida.github.io/urbanplanningdaily/)"
}

RELEVANT = (
    "도시", "국토", "주택", "건축", "부동산", "토지", "교통", "철도", "도로", "지역",
    "재생", "정비", "개발", "계획", "공간", "생활권", "상권", "빈집", "인구", "소멸",
    "균형발전", "스마트시티", "산업단지", "공공주택", "재개발", "재건축", "경관", "농촌",
    "기반시설", "광역", "역세권", "기후", "탄소중립", "녹색건축", "용도지역", "지구단위",
    "공공건축", "도시계획", "건설", "도시공간", "도심", "택지", "국가산단", "상업지역",
    "GTX", "생활인구", "도심융합", "노후계획도시", "철도지하화", "산업전환",
)

STOPWORDS = {
    "연구", "방안", "위한", "관련", "대한", "통한", "기반", "추진", "발표", "개최", "결과",
    "일부개정령안", "일부개정", "개정안", "입법예고", "재입법예고", "보도자료", "보고서",
    "서울시", "국토교통부", "건축공간연구원", "국토연구원", "서울연구원", "정책", "계획",
    "마련", "강화", "지원", "개선", "확대", "제도", "사업", "대응", "활성화", "종합",
    "새로운", "최근", "전국", "정부", "분석", "방향", "현황", "통해", "등의", "관한",
    "기자", "뉴스", "지역", "도시", "국토", "올해", "내년", "한국", "관련해", "대상",
}

TOPICS: dict[str, tuple[str, ...]] = {
    "주택공급·공공주택": ("주택공급", "공공주택", "공급대책", "택지", "청년주택", "임대주택", "분양", "주거복지"),
    "재건축·재개발·정비사업": ("재건축", "재개발", "정비사업", "노후계획도시", "1기 신도시", "도시정비", "소규모주택정비"),
    "지역소멸·균형발전": ("지역소멸", "지방소멸", "균형발전", "생활인구", "인구감소", "소멸위기", "기회발전특구"),
    "철도·광역교통·역세권": ("철도", "GTX", "광역교통", "역세권", "철도지하화", "도시철도", "환승", "고속철도"),
    "도시재생·빈집·원도심": ("도시재생", "빈집", "원도심", "구도심", "쇠퇴지역", "유휴공간", "폐건물", "상권회복"),
    "국토계획·용도지역·규제": ("국토계획", "도시계획", "용도지역", "지구단위계획", "개발제한구역", "그린벨트", "용적률", "규제완화"),
    "산업단지·지역산업 전환": ("산업단지", "국가산단", "산업전환", "기업도시", "산업도시", "첨단산업", "반도체 클러스터", "지역산업"),
    "스마트시티·AI·디지털전환": ("스마트시티", "AI 도시", "인공지능", "디지털트윈", "자율주행", "스마트도시", "도시데이터"),
    "기후위기·탄소중립·녹색건축": ("기후위기", "탄소중립", "녹색건축", "제로에너지", "침수", "폭염", "기후적응", "수해"),
    "상권·골목경제·생활권": ("상권", "골목상권", "생활권", "전통시장", "상업지역", "공실", "젠트리피케이션", "지역상권"),
    "농촌공간·농촌재생": ("농촌공간", "농촌재생", "농촌마을", "농촌소멸", "농촌특화", "농촌협약"),
    "건축정책·공공건축": ("건축정책", "공공건축", "건축물관리", "노후건축물", "건축안전", "건축규제", "건축기준"),
    "토지·부동산시장": ("부동산", "토지거래", "집값", "지가", "공시가격", "전세", "매매가격", "토지시장"),
    "관광·지역개발": ("관광단지", "지역개발", "관광개발", "문화도시", "도시관광", "복합개발", "워케이션"),
    "도시안전·재난 대응": ("도시안전", "재난", "지진", "산사태", "침수", "화재", "안전진단", "붕괴"),
}

DATE_RE = re.compile(r"(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})")
TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalized_date(value: Any) -> str:
    if not value:
        return TODAY.isoformat()
    try:
        dt = dateparser.parse(str(value))
        if dt is None:
            raise ValueError
        return dt.date().isoformat()
    except Exception:
        match = DATE_RE.search(str(value))
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return TODAY.isoformat()


def is_relevant(title: str) -> bool:
    lower = title.lower()
    return any(keyword.lower() in lower for keyword in RELEVANT)


def make_item(title: str, url: str, source: str, category: str, published: Any) -> dict[str, str] | None:
    title = clean(title)
    url = clean(url)
    if len(title) < 5 or not url:
        return None
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": clean(source) or "원문",
        "category": category,
        "date": normalized_date(published),
    }


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response


def google_news(query: str, category: str = "기사", limit: int = 100) -> list[dict[str, str]]:
    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    parsed = feedparser.parse(url, request_headers=HEADERS)
    rows: list[dict[str, str]] = []
    for entry in parsed.entries[:limit]:
        title = clean(entry.get("title", ""))
        if not is_relevant(title):
            continue
        source_data = entry.get("source") or {}
        source = source_data.get("title", "Google 뉴스") if isinstance(source_data, dict) else "Google 뉴스"
        row = make_item(
            title,
            entry.get("link", ""),
            source,
            category,
            entry.get("published") or entry.get("updated"),
        )
        if row:
            rows.append(row)
    return rows


def parse_feed(url: str, source: str, category: str, relevant_only: bool = True, limit: int = 40) -> list[dict[str, str]]:
    parsed = feedparser.parse(url, request_headers=HEADERS)
    rows: list[dict[str, str]] = []
    for entry in parsed.entries[:limit]:
        title = clean(entry.get("title", ""))
        if relevant_only and not is_relevant(title):
            continue
        row = make_item(
            title,
            entry.get("link", ""),
            source,
            category,
            entry.get("published") or entry.get("updated"),
        )
        if row:
            rows.append(row)
    return rows


def nearest_text_with_date(anchor) -> str:
    node = anchor
    for _ in range(8):
        if node is None:
            break
        text = clean(node.get_text(" ", strip=True))
        if DATE_RE.search(text):
            return text
        node = node.parent
    return clean(anchor.parent.get_text(" ", strip=True)) if anchor.parent else ""


def generic_list(
    url: str,
    source: str,
    category: str,
    href_contains: tuple[str, ...],
    relevant_only: bool = True,
    limit: int = 30,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(get(url).text, "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if not any(token in href for token in href_contains):
            continue
        title = clean(anchor.get_text(" ", strip=True))
        if len(title) < 7 or title in {"다운로드", "원문보기", "미리보기", "목록"}:
            continue
        if relevant_only and not is_relevant(title):
            continue
        full_url = urljoin(url, href)
        if full_url in seen:
            continue
        context = nearest_text_with_date(anchor)
        match = DATE_RE.search(context)
        row = make_item(title, full_url, source, category, match.group(0) if match else TODAY.isoformat())
        if row:
            rows.append(row)
            seen.add(full_url)
        if len(rows) >= limit:
            break
    return rows


def parse_lawmaking() -> list[dict[str, str]]:
    url = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"
    soup = BeautifulSoup(get(url).text, "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        title = clean(anchor.get_text(" ", strip=True))
        if "입법예고" not in title or len(title) < 10:
            continue
        context = nearest_text_with_date(anchor)
        if "국토교통부" not in context and not is_relevant(title):
            continue
        full_url = urljoin(url, anchor.get("href", ""))
        if full_url in seen:
            continue
        match = DATE_RE.search(context)
        row = make_item(title, full_url, "국민참여입법센터", "법령", match.group(0) if match else TODAY.isoformat())
        if row:
            rows.append(row)
            seen.add(full_url)
        if len(rows) >= 25:
            break
    return rows


def monthly_windows(months: int = 12) -> list[tuple[date, date]]:
    end = TODAY + timedelta(days=1)
    windows: list[tuple[date, date]] = []
    for _ in range(months):
        start = end - timedelta(days=31)
        windows.append((start, end))
        end = start
    return windows


def historical_backfill() -> list[dict[str, str]]:
    queries = [
        "(도시계획 OR 국토계획 OR 도시재생 OR 재개발 OR 재건축 OR 지구단위계획 OR 용도지역)",
        "(지역소멸 OR 균형발전 OR 주택공급 OR 공공주택 OR 광역교통 OR 철도 OR 산업단지 OR 스마트시티 OR 빈집)",
    ]
    rows: list[dict[str, str]] = []
    for start, end in monthly_windows(12):
        for query in queries:
            dated_query = f"{query} after:{start.isoformat()} before:{end.isoformat()}"
            try:
                found = google_news(dated_query, "기사", 100)
                rows.extend(found)
                print(f"[BACKFILL] {start}~{end}: {len(found)}")
            except Exception as exc:
                print(f"[WARN] backfill {start}~{end}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return rows


def collect_current() -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    errors: list[str] = []
    collectors: list[tuple[str, Callable[[], list[dict[str, str]]]]] = [
        ("최근 뉴스", lambda: google_news(
            "(도시계획 OR 국토계획 OR 도시재생 OR 재개발 OR 재건축 OR 지역소멸 OR 균형발전 OR 공공주택 OR 철도지하화 OR 스마트시티) when:14d",
            "기사", 100
        )),
        ("국토교통부 보도자료", lambda: parse_feed(
            "https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=NEWS",
            "국토교통부", "정책", True, 50
        )),
        ("부처 입법예고", parse_lawmaking),
        ("국토연구원", lambda: generic_list(
            "https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103050000&pub_kind=BR_1",
            "국토연구원", "연구", ("briefView.es", "reportView.es", "view.es"), False, 30
        )),
        ("서울연구원", lambda: generic_list(
            "https://www.si.re.kr/bbs/list.do?key=2024100039",
            "서울연구원", "연구", ("bbs/view.do",), True, 30
        )),
        ("건축공간연구원", lambda: generic_list(
            "https://www.auri.re.kr/publication/list.es?mid=a10312000000&publication_type=research",
            "건축공간연구원", "연구", ("publication/view.es",), True, 30
        )),
    ]
    for name, collector in collectors:
        try:
            rows = collector()
            results.extend(rows)
            print(f"[OK] {name}: {len(rows)}")
        except Exception as exc:
            message = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"[WARN] {message}", file=sys.stderr)
    return results, errors


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def row_date(row: dict[str, str]) -> date:
    try:
        return datetime.fromisoformat(row["date"]).date()
    except Exception:
        return TODAY


def tokenize(title: str) -> list[str]:
    words: list[str] = []
    for token in TOKEN_RE.findall(title):
        token = token.lower()
        if token in STOPWORDS or len(token) < 2 or token.isdigit():
            continue
        words.append(token)
    return words


def keyword_rows(items: list[dict[str, str]], days: int) -> list[dict[str, Any]]:
    cutoff = TODAY - timedelta(days=days - 1)
    counter: Counter[str] = Counter()
    for row in items:
        if row_date(row) >= cutoff:
            counter.update(tokenize(row["title"]))
    return [{"word": word, "count": count} for word, count in counter.most_common(20)]


def topic_match_count(title: str, keywords: tuple[str, ...]) -> int:
    lower = title.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lower)


def issue_rows(items: list[dict[str, str]], days: int) -> list[dict[str, Any]]:
    current_start = TODAY - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    current = [row for row in items if row_date(row) >= current_start]
    previous = [row for row in items if previous_start <= row_date(row) < current_start]

    results: list[dict[str, Any]] = []
    for topic, keywords in TOPICS.items():
        matched = [row for row in current if topic_match_count(row["title"], keywords) > 0]
        if not matched:
            continue
        matched.sort(key=lambda row: (topic_match_count(row["title"], keywords), row["date"]), reverse=True)
        prev_count = sum(1 for row in previous if topic_match_count(row["title"], keywords) > 0)
        current_count = len(matched)

        if days <= 30:
            diff = current_count - prev_count
            if diff > 0:
                trend = f"직전 동일기간보다 {diff}건 증가"
            elif diff < 0:
                trend = f"직전 동일기간보다 {abs(diff)}건 감소"
            else:
                trend = "직전 동일기간과 동일"
        else:
            trend = "최근 1년 누적"

        examples: list[str] = []
        seen_titles: set[str] = set()
        for row in matched:
            title = re.sub(r"\s*-\s*[^-]{2,30}$", "", row["title"]).strip()
            if title in seen_titles:
                continue
            examples.append(title)
            seen_titles.add(title)
            if len(examples) == 3:
                break

        results.append({
            "topic": topic,
            "count": current_count,
            "trend_label": trend,
            "examples": examples,
        })

    results.sort(key=lambda row: (row["count"], len(row["examples"])), reverse=True)
    return results[:10]


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    old_archive = load_json(ARCHIVE_PATH, [])
    if not isinstance(old_archive, list):
        old_archive = []

    new_items, errors = collect_current()
    backfill_done = BACKFILL_PATH.exists()

    if not backfill_done:
        historical = historical_backfill()
        new_items.extend(historical)
        if len(historical) >= 80:
            BACKFILL_PATH.write_text(
                json.dumps({"completed_at": NOW.isoformat(), "items": len(historical)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            errors.append(f"1년 역수집 결과가 {len(historical)}건으로 적어 다음 실행에서 다시 시도합니다.")

    combined: dict[str, dict[str, str]] = {}
    for row in old_archive + new_items:
        if row.get("url"):
            combined[row["url"]] = row

    cutoff = TODAY - timedelta(days=400)
    archive = [row for row in combined.values() if row_date(row) >= cutoff]
    archive.sort(key=lambda row: (row.get("date", ""), row.get("source", "")), reverse=True)

    latest = archive[:100]
    counts = dict(Counter(row["category"] for row in latest))
    updated_at = NOW.strftime("%Y-%m-%d %H:%M KST")

    ARCHIVE_PATH.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    LATEST_PATH.write_text(json.dumps({
        "updated_at": updated_at,
        "counts": counts,
        "errors": errors,
        "items": latest,
        "archive_size": len(archive),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    KEYWORDS_PATH.write_text(json.dumps({
        "updated_at": updated_at,
        "weekly": keyword_rows(archive, 7),
        "monthly": keyword_rows(archive, 30),
        "yearly": keyword_rows(archive, 365),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    ISSUES_PATH.write_text(json.dumps({
        "updated_at": updated_at,
        "weekly": issue_rows(archive, 7),
        "monthly": issue_rows(archive, 30),
        "yearly": issue_rows(archive, 365),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved archive={len(archive)}, latest={len(latest)}, errors={len(errors)}")


if __name__ == "__main__":
    main()
