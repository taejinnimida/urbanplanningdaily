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
BACKFILL_STATE = DATA / "backfill_v3_complete.json"

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
YEAR_CUTOFF = TODAY - timedelta(days=364)
KEEP_CUTOFF = TODAY - timedelta(days=400)

TIMEOUT = 25
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; UrbanPlanningDaily/3.0; "
        "+https://taejinnimida.github.io/urbanplanningdaily/)"
    )
}

RELEVANT = (
    "도시", "국토", "주택", "건축", "부동산", "토지", "교통", "철도", "도로", "지역",
    "재생", "정비", "개발", "계획", "공간", "생활권", "상권", "빈집", "인구", "소멸",
    "균형발전", "스마트시티", "스마트도시", "산업단지", "공공주택", "재개발", "재건축",
    "경관", "농촌", "기반시설", "광역", "역세권", "기후", "탄소중립", "녹색건축",
    "용도지역", "지구단위", "공공건축", "도시계획", "건설", "도시공간", "도심",
    "택지", "국가산단", "상업지역", "GTX", "생활인구", "도심융합", "노후계획도시",
    "철도지하화", "산업전환", "도시혁신", "도시재생", "국가균형발전",
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
    "주택공급·공공주택": (
        "주택공급", "공공주택", "공급대책", "택지", "청년주택", "임대주택", "분양", "주거복지"
    ),
    "재건축·재개발·정비사업": (
        "재건축", "재개발", "정비사업", "노후계획도시", "1기 신도시", "도시정비",
        "소규모주택정비"
    ),
    "지역소멸·균형발전": (
        "지역소멸", "지방소멸", "균형발전", "생활인구", "인구감소", "소멸위기",
        "기회발전특구"
    ),
    "철도·광역교통·역세권": (
        "철도", "GTX", "광역교통", "역세권", "철도지하화", "도시철도", "환승", "고속철도"
    ),
    "도시재생·빈집·원도심": (
        "도시재생", "빈집", "원도심", "구도심", "쇠퇴지역", "유휴공간", "폐건물", "상권회복"
    ),
    "국토계획·용도지역·규제": (
        "국토계획", "도시계획", "용도지역", "지구단위계획", "개발제한구역", "그린벨트",
        "용적률", "규제완화"
    ),
    "산업단지·지역산업 전환": (
        "산업단지", "국가산단", "산업전환", "기업도시", "산업도시", "첨단산업",
        "반도체 클러스터", "지역산업"
    ),
    "스마트시티·AI·디지털전환": (
        "스마트시티", "스마트도시", "AI 도시", "인공지능", "디지털트윈", "자율주행",
        "도시데이터"
    ),
    "기후위기·탄소중립·녹색건축": (
        "기후위기", "탄소중립", "녹색건축", "제로에너지", "침수", "폭염", "기후적응", "수해"
    ),
    "상권·골목경제·생활권": (
        "상권", "골목상권", "생활권", "전통시장", "상업지역", "공실", "젠트리피케이션",
        "지역상권"
    ),
    "농촌공간·농촌재생": (
        "농촌공간", "농촌재생", "농촌마을", "농촌소멸", "농촌특화", "농촌협약"
    ),
    "건축정책·공공건축": (
        "건축정책", "공공건축", "건축물관리", "노후건축물", "건축안전", "건축규제", "건축기준"
    ),
    "토지·부동산시장": (
        "부동산", "토지거래", "집값", "지가", "공시가격", "전세", "매매가격", "토지시장"
    ),
    "관광·지역개발": (
        "관광단지", "지역개발", "관광개발", "문화도시", "도시관광", "복합개발", "워케이션"
    ),
    "도시안전·재난 대응": (
        "도시안전", "재난", "지진", "산사태", "침수", "화재", "안전진단", "붕괴"
    ),
}

NEWS_QUERY_BUNDLES = (
    "(도시계획 OR 국토계획 OR 용도지역 OR 지구단위계획 OR 개발제한구역)",
    "(주택공급 OR 공공주택 OR 재건축 OR 재개발 OR 정비사업 OR 노후계획도시)",
    "(지역소멸 OR 지방소멸 OR 균형발전 OR 생활인구 OR 도시재생 OR 빈집 OR 원도심)",
    "(철도 OR GTX OR 광역교통 OR 역세권 OR 철도지하화 OR 도시철도)",
    "(산업단지 OR 국가산단 OR 스마트시티 OR 스마트도시 OR 도시데이터 OR 도시혁신)",
    "(기후위기 OR 탄소중립 OR 녹색건축 OR 도시침수 OR 기후적응)",
)

DATE_RE = re.compile(r"(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})")
TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_date(value: Any, fallback: date | None = None) -> date | None:
    if value:
        try:
            parsed = dateparser.parse(str(value))
            if parsed:
                return parsed.date()
        except Exception:
            pass

        match = DATE_RE.search(str(value))
        if match:
            try:
                return date(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                )
            except ValueError:
                pass

    return fallback


def normalized_date(value: Any, fallback: date | None = None) -> str | None:
    parsed = parse_date(value, fallback=fallback)
    return parsed.isoformat() if parsed else None


def is_relevant(title: str) -> bool:
    lower = title.lower()
    return any(keyword.lower() in lower for keyword in RELEVANT)


def display_title(title: str) -> str:
    return clean(title)


def title_key(title: str) -> str:
    value = clean(title).lower()
    value = re.sub(r"\s*[-–—]\s*[^-–—]{2,35}$", "", value)
    value = re.sub(r"[^0-9a-z가-힣]+", "", value)
    return value


def make_item(
    title: str,
    url: str,
    source: str,
    category: str,
    published: Any,
    fallback_date: date | None = None,
) -> dict[str, str] | None:
    title = display_title(title)
    url = clean(url)
    item_date = normalized_date(published, fallback=fallback_date)

    if len(title) < 5 or not url or not item_date:
        return None

    try:
        d = date.fromisoformat(item_date)
    except ValueError:
        return None

    if d > TODAY + timedelta(days=1) or d < KEEP_CUTOFF:
        return None

    unique_text = f"{category}|{item_date}|{title_key(title)}"
    return {
        "id": hashlib.sha1(unique_text.encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": clean(source) or "원문",
        "category": category,
        "date": item_date,
    }


def get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    response = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response


def soup_from(url: str, params: dict[str, Any] | None = None) -> BeautifulSoup:
    return BeautifulSoup(get(url, params=params).text, "html.parser")


def nearest_context(anchor, levels: int = 8) -> str:
    node = anchor
    for _ in range(levels):
        if node is None:
            break
        text = clean(node.get_text(" ", strip=True))
        if DATE_RE.search(text):
            return text
        node = node.parent
    if anchor.parent:
        return clean(anchor.parent.get_text(" ", strip=True))
    return ""


def parse_feed(
    url: str,
    source: str,
    category: str,
    relevant_only: bool = True,
    limit: int = 100,
) -> list[dict[str, str]]:
    parsed = feedparser.parse(url, request_headers=HEADERS)
    rows: list[dict[str, str]] = []

    for entry in parsed.entries[:limit]:
        title = clean(entry.get("title", ""))
        if relevant_only and not is_relevant(title):
            continue

        row = make_item(
            title=title,
            url=entry.get("link", ""),
            source=source,
            category=category,
            published=entry.get("published") or entry.get("updated"),
        )
        if row:
            rows.append(row)

    return rows


def google_news(query: str, limit: int = 100) -> list[dict[str, str]]:
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
        if isinstance(source_data, dict):
            source = clean(source_data.get("title", "")) or "Google 뉴스"
        else:
            source = "Google 뉴스"

        row = make_item(
            title=title,
            url=entry.get("link", ""),
            source=source,
            category="기사",
            published=entry.get("published") or entry.get("updated"),
        )
        if row:
            rows.append(row)

    return rows


def month_windows(months: int = 12) -> list[tuple[date, date]]:
    end = TODAY + timedelta(days=1)
    windows: list[tuple[date, date]] = []

    for _ in range(months):
        start = max(YEAR_CUTOFF, end - timedelta(days=31))
        windows.append((start, end))
        end = start
        if end <= YEAR_CUTOFF:
            break

    return windows


def collect_news_backfill() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for start, end in month_windows(12):
        for bundle in NEWS_QUERY_BUNDLES:
            query = f"{bundle} after:{start.isoformat()} before:{end.isoformat()}"
            try:
                found = google_news(query, limit=100)
                rows.extend(found)
                print(
                    f"[NEWS BACKFILL] {start.isoformat()}~{end.isoformat()} "
                    f"{len(found)}"
                )
            except Exception as exc:
                print(
                    f"[WARN] news backfill {start}~{end}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    return rows


def collect_molit_year() -> list[dict[str, str]]:
    base_url = "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    common_params = {
        "lst_gbn": "T",
        "psize": "50",
        "search_gubun": "1",
        "search_regdate_s": YEAR_CUTOFF.isoformat(),
        "search_regdate_e": TODAY.isoformat(),
        "srch_usr_titl": "Y",
    }

    for page in range(1, 35):
        params = dict(common_params)
        params["lcmspage"] = page
        soup = soup_from(base_url, params=params)
        page_rows = 0

        for anchor in soup.select('a[href*="dtl.jsp?id="]'):
            title = clean(anchor.get_text(" ", strip=True))
            if len(title) < 5 or not is_relevant(title):
                continue

            context = nearest_context(anchor)
            item_date = parse_date(context)
            if not item_date or item_date < YEAR_CUTOFF:
                continue

            full_url = urljoin(base_url, anchor.get("href", ""))
            if full_url in seen:
                continue

            row = make_item(
                title=title,
                url=full_url,
                source="국토교통부",
                category="정책",
                published=item_date.isoformat(),
            )
            if row:
                rows.append(row)
                seen.add(full_url)
                page_rows += 1

        print(f"[MOLIT] page={page}, added={page_rows}")

        if page > 2 and page_rows == 0:
            break

    return rows


def generic_paginated(
    url_builder: Callable[[int], str],
    source: str,
    category: str,
    href_tokens: tuple[str, ...],
    max_pages: int,
    relevant_only: bool = True,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    empty_pages = 0

    for page in range(1, max_pages + 1):
        url = url_builder(page)
        soup = soup_from(url)
        page_rows = 0
        dates_seen: list[date] = []

        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if not any(token in href for token in href_tokens):
                continue

            title = clean(anchor.get_text(" ", strip=True))
            if len(title) < 7 or title in {"다운로드", "원문보기", "미리보기", "목록"}:
                continue
            if relevant_only and not is_relevant(title):
                continue

            context = nearest_context(anchor)
            item_date = parse_date(context)
            if not item_date:
                continue

            dates_seen.append(item_date)
            if item_date < YEAR_CUTOFF:
                continue

            full_url = urljoin(url, href)
            if full_url in seen:
                continue

            row = make_item(
                title=title,
                url=full_url,
                source=source,
                category=category,
                published=item_date.isoformat(),
            )
            if row:
                rows.append(row)
                seen.add(full_url)
                page_rows += 1

        print(f"[{source}] page={page}, added={page_rows}")

        if page_rows == 0:
            empty_pages += 1
        else:
            empty_pages = 0

        if dates_seen and min(dates_seen) < YEAR_CUTOFF:
            break
        if empty_pages >= 3:
            break

    return rows


def collect_krihs_year() -> list[dict[str, str]]:
    return generic_paginated(
        url_builder=lambda page: (
            "https://www.krihs.re.kr/gallery.es"
            f"?bid=0022&mid=a10103050000&nPage={page}"
        ),
        source="국토연구원",
        category="연구",
        href_tokens=("view.es", "galleryView.es"),
        max_pages=20,
        relevant_only=False,
    )


def collect_auri_year() -> list[dict[str, str]]:
    return generic_paginated(
        url_builder=lambda page: (
            "https://www.auri.re.kr/publication/list.es"
            "?mid=a10312000000&publication_type=research"
            f"&nPage={page}"
        ),
        source="건축공간연구원",
        category="연구",
        href_tokens=("publication/view.es",),
        max_pages=30,
        relevant_only=True,
    )


def collect_si_year() -> list[dict[str, str]]:
    return generic_paginated(
        url_builder=lambda page: (
            "https://www.si.re.kr/bbs/list.do"
            f"?key=2024100039&pageIndex={page}"
        ),
        source="서울연구원",
        category="연구",
        href_tokens=("bbs/view.do",),
        max_pages=30,
        relevant_only=True,
    )


def collect_recent_laws_year() -> list[dict[str, str]]:
    base_url = "https://www.law.go.kr/nwRvsLsPop.do"
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for page in range(1, 40):
        params = {
            "chrIdx": "0",
            "cptOfi": "1613000",
            "pg": str(page),
        }
        soup = soup_from(base_url, params=params)
        page_rows = 0
        page_dates: list[date] = []

        for table_row in soup.select("tr"):
            context = clean(table_row.get_text(" ", strip=True))
            if "국토교통부" not in context:
                continue

            anchor = table_row.select_one("a[href]")
            if not anchor:
                continue

            title = clean(anchor.get_text(" ", strip=True))
            if not is_relevant(title):
                continue

            item_date = parse_date(context)
            if not item_date:
                continue

            page_dates.append(item_date)
            if item_date < YEAR_CUTOFF:
                continue

            full_url = urljoin(base_url, anchor.get("href", ""))
            if full_url in seen:
                continue

            row = make_item(
                title=title,
                url=full_url,
                source="국가법령정보센터",
                category="법령",
                published=item_date.isoformat(),
            )
            if row:
                rows.append(row)
                seen.add(full_url)
                page_rows += 1

        print(f"[LAW] page={page}, added={page_rows}")

        if page_dates and min(page_dates) < YEAR_CUTOFF:
            break
        if page > 3 and page_rows == 0:
            break

    return rows


def collect_lawmaking_current() -> list[dict[str, str]]:
    url = "https://opinion.lawmaking.go.kr/gcom/ogLmPp"
    soup = soup_from(url)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.select("a[href]"):
        title = clean(anchor.get_text(" ", strip=True))
        if "입법예고" not in title or len(title) < 10:
            continue

        context = nearest_context(anchor)
        if "국토교통부" not in context and not is_relevant(title):
            continue

        full_url = urljoin(url, anchor.get("href", ""))
        if full_url in seen:
            continue

        item_date = parse_date(context, fallback=TODAY)
        row = make_item(
            title=title,
            url=full_url,
            source="국민참여입법센터",
            category="법령",
            published=item_date.isoformat() if item_date else TODAY.isoformat(),
        )
        if row:
            rows.append(row)
            seen.add(full_url)

    return rows[:50]


def collect_current() -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    errors: list[str] = []

    collectors: list[tuple[str, Callable[[], list[dict[str, str]]]]] = [
        (
            "최근 뉴스",
            lambda: google_news(
                "(도시계획 OR 국토계획 OR 도시재생 OR 재개발 OR 재건축 "
                "OR 지역소멸 OR 균형발전 OR 공공주택 OR 철도지하화 "
                "OR 스마트시티) when:14d",
                limit=100,
            ),
        ),
        (
            "국토교통부 RSS",
            lambda: parse_feed(
                "https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=NEWS",
                source="국토교통부",
                category="정책",
                relevant_only=True,
                limit=100,
            ),
        ),
        ("입법예고", collect_lawmaking_current),
    ]

    for name, collector in collectors:
        try:
            rows = collector()
            results.extend(rows)
            print(f"[CURRENT OK] {name}: {len(rows)}")
        except Exception as exc:
            message = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"[WARN] {message}", file=sys.stderr)

    return results, errors


def collect_one_year_backfill() -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    errors: list[str] = []

    collectors: list[tuple[str, Callable[[], list[dict[str, str]]]]] = [
        ("뉴스 1년", collect_news_backfill),
        ("국토교통부 1년", collect_molit_year),
        ("최근 공포법령 1년", collect_recent_laws_year),
        ("국토연구원 1년", collect_krihs_year),
        ("서울연구원 1년", collect_si_year),
        ("건축공간연구원 1년", collect_auri_year),
    ]

    for name, collector in collectors:
        try:
            rows = collector()
            results.extend(rows)
            print(f"[BACKFILL OK] {name}: {len(rows)}")
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


def row_date(row: dict[str, str]) -> date | None:
    try:
        return date.fromisoformat(row["date"])
    except Exception:
        return None


def deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}

    for row in rows:
        item_date = row_date(row)
        if not item_date or item_date < KEEP_CUTOFF or item_date > TODAY + timedelta(days=1):
            continue

        key = f"{row.get('category', '')}|{row['date']}|{title_key(row.get('title', ''))}"
        old = by_key.get(key)

        if old is None:
            by_key[key] = row
            continue

        # 같은 기사라면 Google 뉴스 중계주소보다 원 기관 주소를 우선합니다.
        old_is_google = "news.google.com" in old.get("url", "")
        new_is_google = "news.google.com" in row.get("url", "")
        if old_is_google and not new_is_google:
            by_key[key] = row

    return list(by_key.values())


def tokenize(title: str) -> list[str]:
    words: list[str] = []

    for token in TOKEN_RE.findall(title):
        token = token.lower()
        if token in STOPWORDS or len(token) < 2 or token.isdigit():
            continue
        words.append(token)

    return words


def rows_for_days(items: list[dict[str, str]], days: int) -> list[dict[str, str]]:
    cutoff = TODAY - timedelta(days=days - 1)
    return [row for row in items if row_date(row) and row_date(row) >= cutoff]


def category_counts(items: list[dict[str, str]], days: int) -> dict[str, int]:
    return dict(Counter(row["category"] for row in rows_for_days(items, days)))


def keyword_rows(items: list[dict[str, str]], days: int) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()

    for row in rows_for_days(items, days):
        counter.update(tokenize(row["title"]))

    return [
        {"word": word, "count": count}
        for word, count in counter.most_common(20)
    ]


def topic_match_count(title: str, keywords: tuple[str, ...]) -> int:
    lower = title.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lower)


def issue_rows(items: list[dict[str, str]], days: int) -> list[dict[str, Any]]:
    current_start = TODAY - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)

    current = [
        row for row in items
        if row_date(row) and row_date(row) >= current_start
    ]
    previous = [
        row for row in items
        if row_date(row)
        and previous_start <= row_date(row) < current_start
    ]

    results: list[dict[str, Any]] = []

    for topic, keywords in TOPICS.items():
        matched = [
            row for row in current
            if topic_match_count(row["title"], keywords) > 0
        ]
        if not matched:
            continue

        matched.sort(
            key=lambda row: (
                topic_match_count(row["title"], keywords),
                row["date"],
            ),
            reverse=True,
        )

        previous_count = sum(
            1 for row in previous
            if topic_match_count(row["title"], keywords) > 0
        )
        current_count = len(matched)

        if days <= 30:
            difference = current_count - previous_count
            if difference > 0:
                trend = f"직전 동일기간보다 {difference}건 증가"
            elif difference < 0:
                trend = f"직전 동일기간보다 {abs(difference)}건 감소"
            else:
                trend = "직전 동일기간과 동일"
        else:
            trend = "최근 1년 누적"

        examples: list[str] = []
        seen_titles: set[str] = set()

        for row in matched:
            title = re.sub(
                r"\s*[-–—]\s*[^-–—]{2,35}$",
                "",
                row["title"],
            ).strip()
            if title in seen_titles:
                continue
            examples.append(title)
            seen_titles.add(title)
            if len(examples) == 3:
                break

        results.append(
            {
                "topic": topic,
                "count": current_count,
                "trend_label": trend,
                "examples": examples,
            }
        )

    results.sort(
        key=lambda row: (row["count"], len(row["examples"])),
        reverse=True,
    )
    return results[:10]


def coverage(items: list[dict[str, str]]) -> dict[str, Any]:
    year_items = rows_for_days(items, 365)
    dates = [row_date(row) for row in year_items if row_date(row)]

    if not dates:
        return {
            "items": 0,
            "oldest": None,
            "newest": None,
            "days_covered": 0,
            "complete": False,
        }

    oldest = min(dates)
    newest = max(dates)
    days_covered = (newest - oldest).days + 1
    complete = len(year_items) >= 300 and oldest <= TODAY - timedelta(days=330)

    return {
        "items": len(year_items),
        "oldest": oldest.isoformat(),
        "newest": newest.isoformat(),
        "days_covered": days_covered,
        "complete": complete,
    }


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    old_archive = load_json(ARCHIVE_PATH, [])
    if not isinstance(old_archive, list):
        old_archive = []

    current_rows, errors = collect_current()
    all_rows = old_archive + current_rows

    state = load_json(BACKFILL_STATE, {})
    needs_backfill = not bool(state.get("complete"))

    if needs_backfill:
        historical_rows, historical_errors = collect_one_year_backfill()
        all_rows.extend(historical_rows)
        errors.extend(historical_errors)

    archive = deduplicate(all_rows)
    archive.sort(
        key=lambda row: (row.get("date", ""), row.get("source", "")),
        reverse=True,
    )

    year_items = rows_for_days(archive, 365)
    latest_items = year_items[:100]
    report_coverage = coverage(archive)

    # 충분한 1년 범위가 확인됐을 때만 역수집 완료로 표시합니다.
    if report_coverage["complete"]:
        BACKFILL_STATE.write_text(
            json.dumps(
                {
                    "complete": True,
                    "completed_at": NOW.isoformat(),
                    "coverage": report_coverage,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    updated_at = NOW.strftime("%Y-%m-%d %H:%M KST")

    ARCHIVE_PATH.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 홈페이지 상단의 분야별 건수는 최근 1년 전체 기준입니다.
    LATEST_PATH.write_text(
        json.dumps(
            {
                "updated_at": updated_at,
                "counts": category_counts(archive, 365),
                "period_counts": {
                    "weekly": category_counts(archive, 7),
                    "monthly": category_counts(archive, 30),
                    "yearly": category_counts(archive, 365),
                },
                "errors": errors,
                "items": latest_items,
                "archive_size": len(year_items),
                "coverage": report_coverage,
                "counts_period": "최근 1년",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    KEYWORDS_PATH.write_text(
        json.dumps(
            {
                "updated_at": updated_at,
                "weekly": keyword_rows(archive, 7),
                "monthly": keyword_rows(archive, 30),
                "yearly": keyword_rows(archive, 365),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    ISSUES_PATH.write_text(
        json.dumps(
            {
                "updated_at": updated_at,
                "coverage": report_coverage,
                "period_counts": {
                    "weekly": category_counts(archive, 7),
                    "monthly": category_counts(archive, 30),
                    "yearly": category_counts(archive, 365),
                },
                "weekly": issue_rows(archive, 7),
                "monthly": issue_rows(archive, 30),
                "yearly": issue_rows(archive, 365),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "Saved "
        f"year_items={len(year_items)}, "
        f"oldest={report_coverage['oldest']}, "
        f"days={report_coverage['days_covered']}, "
        f"complete={report_coverage['complete']}, "
        f"errors={len(errors)}"
    )


if __name__ == "__main__":
    main()
