from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
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

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UrbanPlanningDaily/1.0; +https://taejinnimida.github.io/urbanplanningdaily/)"
}
TIMEOUT = 25

RELEVANT = (
    "도시", "국토", "주택", "건축", "부동산", "토지", "교통", "철도", "도로", "지역",
    "재생", "정비", "개발", "계획", "공간", "생활권", "상권", "빈집", "인구", "소멸",
    "균형발전", "스마트시티", "산업단지", "공공주택", "재개발", "재건축", "경관", "농촌",
    "기반시설", "광역", "역세권", "기후", "탄소중립", "녹색건축", "용도지역", "지구단위",
    "공공건축", "도시계획", "건설", "도시공간", "도심", "택지", "국가산단", "상업지역",
)

STOPWORDS = {
    "연구", "방안", "위한", "관련", "대한", "통한", "기반", "추진", "발표", "개최", "결과",
    "일부개정령안", "일부개정", "개정안", "입법예고", "재입법예고", "보도자료", "보고서",
    "서울시", "국토교통부", "건축공간연구원", "국토연구원", "서울연구원", "정책", "계획",
    "마련", "강화", "지원", "개선", "확대", "제도", "사업", "대응", "활성화", "종합",
    "새로운", "최근", "전국", "정부", "분석", "방향", "현황", "통해", "등의", "관한",
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
        m = DATE_RE.search(str(value))
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return TODAY.isoformat()


def is_relevant(title: str) -> bool:
    return any(k.lower() in title.lower() for k in RELEVANT)


def item(title: str, url: str, source: str, category: str, date: Any) -> dict[str, str] | None:
    title = clean(title)
    url = clean(url)
    if len(title) < 5 or not url:
        return None
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": source,
        "category": category,
        "date": normalized_date(date),
    }


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response


def parse_feed(url: str, source: str, category: str, relevant_only: bool = True, limit: int = 30) -> list[dict[str, str]]:
    parsed = feedparser.parse(url, request_headers=HEADERS)
    rows: list[dict[str, str]] = []
    for entry in parsed.entries[:limit]:
        title = clean(entry.get("title", ""))
        if relevant_only and not is_relevant(title):
            continue
        row = item(
            title,
            entry.get("link", ""),
            source,
            category,
            entry.get("published") or entry.get("updated") or TODAY.isoformat(),
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
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not any(token in href for token in href_contains):
            continue
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 7 or title in {"다운로드", "원문보기", "미리보기", "목록"}:
            continue
        if relevant_only and not is_relevant(title):
            continue
        full_url = urljoin(url, href)
        if full_url in seen:
            continue
        context = nearest_text_with_date(a)
        m = DATE_RE.search(context)
        date = m.group(0) if m else TODAY.isoformat()
        row = item(title, full_url, source, category, date)
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
    for a in soup.select("a[href]"):
        title = clean(a.get_text(" ", strip=True))
        if "입법예고" not in title or len(title) < 10:
            continue
        context = nearest_text_with_date(a)
        if "국토교통부" not in context and not is_relevant(title):
            continue
        full_url = urljoin(url, a.get("href", ""))
        if full_url in seen:
            continue
        m = DATE_RE.search(context)
        row = item(title, full_url, "국민참여입법센터", "법령", m.group(0) if m else TODAY.isoformat())
        if row:
            rows.append(row)
            seen.add(full_url)
        if len(rows) >= 25:
            break
    return rows


def parse_recent_laws() -> list[dict[str, str]]:
    url = "https://www.law.go.kr/nwRvsLsPop.do?chrIdx=0&cptOfi=1613000&lsKndCd=&pg=1"
    soup = BeautifulSoup(get(url).text, "html.parser")
    rows: list[dict[str, str]] = []
    for tr in soup.select("tr"):
        text = clean(tr.get_text(" ", strip=True))
        if "국토교통부" not in text:
            continue
        a = tr.select_one("a[href]")
        if not a:
            continue
        title = clean(a.get_text(" ", strip=True))
        if not is_relevant(title):
            continue
        dates = DATE_RE.findall(text)
        date = "-".join([dates[0][0], f"{int(dates[0][1]):02d}", f"{int(dates[0][2]):02d}"]) if dates else TODAY.isoformat()
        row = item(title, urljoin(url, a.get("href", "")), "국가법령정보센터", "법령", date)
        if row:
            rows.append(row)
        if len(rows) >= 25:
            break
    return rows


def collect_all() -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    errors: list[str] = []

    collectors = [
        ("국토교통부 보도자료", lambda: parse_feed(
            "https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=NEWS",
            "국토교통부", "정책", True, 40
        )),
        ("도시계획 뉴스", lambda: parse_feed(
            "https://news.google.com/rss/search?q=" + quote(
                '(도시계획 OR 국토계획 OR 도시재생 OR 재개발 OR 재건축 OR 지역소멸 OR 지구단위계획) when:7d'
            ) + "&hl=ko&gl=KR&ceid=KR:ko",
            "Google 뉴스", "기사", True, 40
        )),
        ("부처 입법예고", parse_lawmaking),
        ("최근 공포법령", parse_recent_laws),
        ("국토연구원 국토정책 Brief", lambda: generic_list(
            "https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103050000&pub_kind=BR_1",
            "국토연구원", "연구", ("briefView.es", "reportView.es", "view.es"), False, 20
        )),
        ("서울연구원 연구보고서", lambda: generic_list(
            "https://www.si.re.kr/bbs/list.do?key=2024100039",
            "서울연구원", "연구", ("bbs/view.do",), True, 20
        )),
        ("건축공간연구원 연구보고서", lambda: generic_list(
            "https://www.auri.re.kr/publication/list.es?mid=a10312000000&publication_type=research",
            "건축공간연구원", "연구", ("publication/view.es",), True, 20
        )),
        ("건축공간연구원 보도자료", lambda: generic_list(
            "https://www.auri.re.kr/board.es?mid=a10401030000&bid=0013",
            "건축공간연구원", "정책", ("boardView.es", "board.es?act=view"), True, 20
        )),
    ]

    for name, fn in collectors:
        try:
            rows = fn()
            results.extend(rows)
            print(f"[OK] {name}: {len(rows)}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"[WARN] {errors[-1]}", file=sys.stderr)

    dedup: dict[str, dict[str, str]] = {}
    for row in results:
        dedup[row["url"]] = row
    return list(dedup.values()), errors


def load_archive() -> list[dict[str, str]]:
    if not ARCHIVE_PATH.exists():
        return []
    try:
        data = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def tokenize(title: str) -> list[str]:
    words = []
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
        try:
            d = datetime.fromisoformat(row["date"]).date()
        except Exception:
            continue
        if d >= cutoff:
            counter.update(tokenize(row["title"]))
    return [{"word": word, "count": count} for word, count in counter.most_common(20)]


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    new_items, errors = collect_all()
    old_items = load_archive()

    combined: dict[str, dict[str, str]] = {x["url"]: x for x in old_items if x.get("url")}
    for row in new_items:
        combined[row["url"]] = row

    cutoff = TODAY - timedelta(days=370)
    archive = []
    for row in combined.values():
        try:
            d = datetime.fromisoformat(row["date"]).date()
        except Exception:
            d = TODAY
        if d >= cutoff:
            archive.append(row)

    archive.sort(key=lambda x: (x.get("date", ""), x.get("source", "")), reverse=True)
    latest = archive[:80]
    counts = dict(Counter(x["category"] for x in latest))

    updated_at = NOW.strftime("%Y-%m-%d %H:%M KST")
    ARCHIVE_PATH.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    LATEST_PATH.write_text(json.dumps({
        "updated_at": updated_at,
        "counts": counts,
        "errors": errors,
        "items": latest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    KEYWORDS_PATH.write_text(json.dumps({
        "updated_at": updated_at,
        "weekly": keyword_rows(archive, 7),
        "monthly": keyword_rows(archive, 30),
        "yearly": keyword_rows(archive, 365),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved archive={len(archive)}, latest={len(latest)}, errors={len(errors)}")


if __name__ == "__main__":
    main()
