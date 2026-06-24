from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import feedparser
import requests
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

ARCHIVE_PATH = DATA / "archive.json"
LATEST_PATH = DATA / "latest.json"
KEYWORDS_PATH = DATA / "keywords.json"
ISSUES_PATH = DATA / "issues.json"
STATE_PATH = DATA / "state.json"

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
KEEP_START = TODAY - timedelta(days=400)
YEAR_START = TODAY - timedelta(days=364)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/149 Safari/537.36"
    )
}

# 공개된 공식 누리집 안의 자료만 검색하도록 도메인을 고정합니다.
OFFICIAL_POLICY_QUERIES = [
    (
        "국토교통부",
        "site:molit.go.kr/USR/NEWS/m_71/dtl.jsp "
        "(도시 OR 국토 OR 주택 OR 건축 OR 토지 OR 교통 OR 철도 OR 지역)",
    ),
    (
        "서울특별시",
        "site:seoul.go.kr/news/news_report.do "
        "(도시 OR 주택 OR 건축 OR 토지 OR 교통 OR 환경 OR 상권 OR 안전)",
    ),
    (
        "경기도",
        "site:gnews.gg.go.kr/briefing/brief_gongbo_view.do "
        "(도시 OR 주택 OR 건축 OR 토지 OR 교통 OR 환경 OR 산업단지 OR 지역)",
    ),
]

OTHER_QUERIES = [
    (
        "법령",
        "법령·입법",
        "(site:opinion.lawmaking.go.kr OR site:law.go.kr) "
        "(도시 OR 국토 OR 주택 OR 건축 OR 토지 OR 교통)",
    ),
    (
        "연구",
        "연구기관",
        "(site:krihs.re.kr OR site:si.re.kr OR site:auri.re.kr) "
        "(도시 OR 국토 OR 주택 OR 건축 OR 토지 OR 지역 OR 교통)",
    ),
    (
        "기사",
        "언론",
        "(도시계획 OR 국토계획 OR 지구단위계획 OR 용도지역 "
        "OR 도시개발 OR 도시재생 OR 재개발 OR 재건축 OR 지역소멸 "
        "OR 균형발전 OR 공공주택 OR 철도지하화 OR 스마트시티)",
    ),
]

RELEVANT_WORDS = (
    "도시", "국토", "주택", "건축", "부동산", "토지", "교통", "철도",
    "도로", "지역", "재생", "정비", "개발", "계획", "공간", "생활권",
    "상권", "빈집", "인구", "소멸", "균형발전", "스마트시티",
    "스마트도시", "산업단지", "공공주택", "재개발", "재건축", "경관",
    "농촌", "기반시설", "광역", "역세권", "기후", "탄소중립",
    "녹색건축", "용도지역", "지구단위", "공공건축", "도심", "택지",
    "국가산단", "상업지역", "GTX", "생활인구", "도심융합",
    "노후계획도시", "철도지하화", "산업전환", "도시혁신", "주거복지",
    "공실", "침수", "폭염",
)

STOPWORDS = {
    "연구", "방안", "위한", "관련", "대한", "통한", "기반", "추진",
    "발표", "개최", "결과", "입법예고", "보도자료", "보고서", "서울시",
    "경기도", "국토교통부", "국토연구원", "서울연구원", "건축공간연구원",
    "정책", "계획", "마련", "강화", "지원", "개선", "확대", "제도",
    "사업", "대응", "활성화", "종합", "새로운", "최근", "전국", "정부",
    "분석", "방향", "현황", "통해", "관한", "기자", "뉴스", "지역",
    "도시", "국토", "올해", "내년", "한국", "대상",
}

TOPICS = {
    "주택공급·공공주택": (
        "주택공급", "공공주택", "택지", "임대주택", "주거복지", "분양"
    ),
    "재건축·재개발·정비사업": (
        "재건축", "재개발", "정비사업", "노후계획도시", "1기 신도시"
    ),
    "지역소멸·균형발전": (
        "지역소멸", "지방소멸", "균형발전", "생활인구", "인구감소"
    ),
    "철도·광역교통·역세권": (
        "철도", "GTX", "광역교통", "역세권", "철도지하화", "도시철도"
    ),
    "도시재생·빈집·원도심": (
        "도시재생", "빈집", "원도심", "구도심", "유휴공간", "공실"
    ),
    "국토계획·용도지역·규제": (
        "국토계획", "도시계획", "용도지역", "지구단위계획",
        "개발제한구역", "그린벨트", "용적률"
    ),
    "산업단지·지역산업 전환": (
        "산업단지", "국가산단", "산업전환", "기업도시", "첨단산업"
    ),
    "스마트시티·AI·디지털전환": (
        "스마트시티", "스마트도시", "인공지능", "디지털트윈",
        "자율주행", "도시데이터"
    ),
    "기후위기·탄소중립·녹색건축": (
        "기후위기", "탄소중립", "녹색건축", "제로에너지",
        "침수", "폭염", "기후적응"
    ),
    "상권·골목경제·생활권": (
        "상권", "골목상권", "생활권", "전통시장", "공실",
        "젠트리피케이션"
    ),
    "농촌공간·농촌재생": (
        "농촌공간", "농촌재생", "농촌마을", "농촌소멸", "농촌협약"
    ),
    "건축정책·공공건축": (
        "건축정책", "공공건축", "건축물관리", "노후건축물", "건축안전"
    ),
    "토지·부동산시장": (
        "부동산", "토지거래", "집값", "지가", "공시가격", "전세",
        "매매가격"
    ),
    "관광·지역개발": (
        "관광단지", "지역개발", "관광개발", "문화도시", "복합개발",
        "워케이션"
    ),
    "도시안전·재난 대응": (
        "도시안전", "재난", "지진", "산사태", "침수", "화재",
        "안전진단", "붕괴"
    ),
}

TOKEN_RE = re.compile(r"[가-힣]{2,}")

# 기관명·언론사명이 아니라 실제 도시계획 이슈가 집계되도록
# 의미 있는 도시·건축·국토 분야 용어를 우선 집계합니다.
KEYWORD_PHRASES = {
    "재건축": ("재건축",),
    "재개발": ("재개발",),
    "정비사업": ("정비사업", "도시정비"),
    "노후계획도시": ("노후계획도시", "1기 신도시"),
    "주택공급": ("주택공급", "주택 공급"),
    "공공주택": ("공공주택", "공공임대"),
    "임대주택": ("임대주택",),
    "주거복지": ("주거복지",),
    "도시재생": ("도시재생",),
    "빈집": ("빈집",),
    "원도심": ("원도심", "구도심"),
    "지역소멸": ("지역소멸", "지방소멸"),
    "균형발전": ("균형발전",),
    "생활인구": ("생활인구",),
    "인구감소지역": ("인구감소지역",),
    "도시계획": ("도시계획",),
    "국토계획": ("국토계획",),
    "토지이용": ("토지이용",),
    "지구단위계획": ("지구단위계획",),
    "용도지역": ("용도지역",),
    "개발제한구역": ("개발제한구역", "그린벨트"),
    "용적률": ("용적률",),
    "공공기여": ("공공기여",),
    "도시개발": ("도시개발",),
    "역세권": ("역세권",),
    "광역교통": ("광역교통",),
    "GTX": ("gtx",),
    "철도지하화": ("철도지하화",),
    "도시철도": ("도시철도",),
    "산업단지": ("산업단지", "국가산단"),
    "도시공업지역": ("도시공업지역",),
    "도심융합특구": ("도심융합특구",),
    "기회발전특구": ("기회발전특구",),
    "스마트시티": ("스마트시티", "스마트도시"),
    "디지털트윈": ("디지털트윈",),
    "공공건축": ("공공건축",),
    "건축물관리": ("건축물관리", "노후건축물"),
    "녹색건축": ("녹색건축", "제로에너지"),
    "탄소중립": ("탄소중립",),
    "기후위기": ("기후위기", "기후적응"),
    "침수": ("침수",),
    "폭염": ("폭염",),
    "골목상권": ("골목상권",),
    "전통시장": ("전통시장",),
    "농촌공간": ("농촌공간",),
    "농촌재생": ("농촌재생",),
    "토지거래": ("토지거래",),
    "부동산시장": ("부동산시장", "주택시장"),
    "공시가격": ("공시가격",),
    "관광단지": ("관광단지",),
    "복합개발": ("복합개발",),
    "경관": ("경관",),
    "문화재": ("문화재", "국가유산"),
    "기반시설": ("기반시설",),
}

# 의미가 약한 행정용어와 기관·언론사·사이트 명칭은 후보에서 제외합니다.
KEYWORD_NOISE = STOPWORDS | {
    "국가법령정보센터", "국민참여입법센터", "한국주택경제신문",
    "서울연구데이터서비스", "하우징헤럴드", "경기도뉴스포털",
    "연합뉴스", "서울특별시", "서울특별시청", "대한민국정책브리핑",
    "정책브리핑", "머니투데이", "매일경제", "한국경제", "조선일보",
    "중앙일보", "동아일보", "한겨레", "경향신문", "뉴스1", "뉴시스",
    "조례", "자치법규", "행정규칙", "구역", "선정", "지정", "고시",
    "공고", "일부개정", "전부개정", "개정안", "폐지", "시행",
    "입법", "예고", "의견", "제출", "알림", "모집", "공모", "접수",
    "보도", "자료", "센터", "포털", "서비스", "신문", "헤럴드",
}


# 자료 품질 필터
# 1) 연구기관의 단순 사진·행사 스케치 게시물은 제외
# 2) 법령명이 없이 '변경 조문' 등만 표시된 자료는 제외
RESEARCH_MEDIA_PATTERNS = (
    r"^\s*\[(?:포토|사진|photo)\]",
    r"^\s*(?:포토\s*뉴스|포토\s*갤러리|포토\s*앨범)\b",
    r"사진으로\s*보는",
    r"(?:현장|행사|세미나|포럼)\s*스케치",
    r"(?:행사|현장)\s*사진",
    r"사진\s*(?:공유|모음|자료|갤러리|앨범)",
)

LAW_GENERIC_PHRASES = (
    "변경 조문",
    "변경조문",
    "신구 조문 대비표",
    "신구조문대비표",
    "개정문",
    "제정·개정 이유",
    "제정개정이유",
    "조문 정보",
    "조문정보",
    "법령 체계도",
    "법령체계도",
)

LAW_NAME_SIGNALS = (
    "법률", "특별법", "기본법", "시행령", "시행규칙",
    "조례", "규정", "기준", "지침",
)

FILTER_COUNTS: Counter[str] = Counter()


# 무료 자동 이슈 분석 규칙
# 기사 본문 생성형 요약이 아니라, 관련 자료 10~15건의 제목에서
# 반복되는 변화·쟁점·도시계획적 영향을 추출합니다.
ACTION_SIGNALS = {
    "추진": "사업·정책 추진",
    "확대": "대상·지원 확대",
    "완화": "규제·기준 완화",
    "강화": "관리·규제 강화",
    "도입": "새 제도 도입",
    "지정": "구역·사업 지정",
    "공급": "공급 확대",
    "개정": "법·제도 개정",
    "시행": "제도 시행",
    "발표": "정책 발표",
    "검토": "정책·사업 검토",
    "착공": "사업 착공",
    "준공": "사업 완료",
    "유치": "기능·기업 유치",
    "지원": "재정·행정 지원",
    "정비": "정비·개선 추진",
    "조성": "공간·시설 조성",
    "계획": "계획 수립",
}

ISSUE_SIGNALS = {
    "갈등": "이해관계자 갈등",
    "반발": "주민·이해관계자 반발",
    "반대": "반대 여론",
    "지연": "사업 지연",
    "분담금": "분담금 부담",
    "공사비": "공사비 상승",
    "사업성": "사업성 확보",
    "소송": "법적 분쟁",
    "미분양": "미분양 위험",
    "공실": "공실 증가",
    "침체": "시장 침체",
    "부족": "공급·재원 부족",
    "부담": "비용 부담",
    "논란": "정책·사업 논란",
    "취소": "사업 취소·철회",
    "무산": "사업 무산 위험",
}

IMPACT_SIGNALS = {
    "주택공급": "주택 공급과 주거 선택",
    "공공주택": "공공주택 공급과 주거복지",
    "재건축": "노후 주거지 정비와 사업성",
    "재개발": "기성시가지 정비와 원주민 재정착",
    "용적률": "도시 밀도와 사업성",
    "역세권": "거점과 생활권 재편",
    "광역교통": "광역 접근성과 생활권 확대",
    "철도": "교통축과 역세권 구조",
    "상권": "상권과 지역경제",
    "산업단지": "산업입지와 일자리",
    "인구": "인구 변화와 생활권 유지",
    "소멸": "축소지역의 기능 유지",
    "빈집": "유휴공간과 주거지 관리",
    "도시재생": "기성시가지 관리와 기능 전환",
    "토지": "토지이용과 개발 압력",
    "부동산": "부동산시장과 개발 기대",
    "기후": "기후위기 대응과 공간 안전",
    "침수": "도시 안전과 방재",
    "공공기여": "개발이익과 공공성 배분",
    "경관": "경관 관리와 개발 규제",
    "농촌": "농촌 생활권과 서비스 유지",
}

TOPIC_IMPACT_DEFAULTS = {
    "주택공급·공공주택": "주택 공급, 주거 선택과 주거복지",
    "재건축·재개발·정비사업": "노후 주거지 정비, 사업성과 주민 부담",
    "지역소멸·균형발전": "생활권 유지, 공공서비스와 지역 기능 재편",
    "철도·광역교통·역세권": "광역 접근성, 역세권과 도시공간 구조",
    "도시재생·빈집·원도심": "기성시가지 관리, 유휴공간과 상권 회복",
    "국토계획·용도지역·규제": "토지이용, 개발밀도와 공공성",
    "산업단지·지역산업 전환": "산업입지, 일자리와 지역경제",
    "스마트시티·AI·디지털전환": "도시관리 방식과 공공서비스 효율",
    "기후위기·탄소중립·녹색건축": "도시 안전, 에너지와 기후적응",
    "상권·골목경제·생활권": "생활권 경제, 공실과 지역상권 유지",
    "농촌공간·농촌재생": "농촌 생활권, 정주서비스와 유휴공간",
    "건축정책·공공건축": "건축물 생애주기와 공공공간 품질",
    "토지·부동산시장": "토지이용, 주택시장과 개발 압력",
    "관광·지역개발": "지역 기능 전환, 관광수요와 생활환경",
    "도시안전·재난 대응": "방재, 기반시설과 취약지역 관리",
}


def top_signal_labels(
    titles: list[str],
    mapping: dict[str, str],
    limit: int = 2,
) -> list[str]:
    counter: Counter[str] = Counter()
    for title in titles:
        lower = title.lower()
        for signal, label in mapping.items():
            if signal.lower() in lower:
                counter[label] += 1
    return [label for label, _ in counter.most_common(limit)]


def join_labels(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return "·".join(labels)


def diversify_issue_rows(
    matched: list[dict[str, str]],
    limit: int = 15,
) -> list[dict[str, str]]:
    """같은 출처가 요약을 독점하지 않도록 출처별 최대 2건만 반영합니다."""
    selected: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    seen_titles: set[str] = set()

    for row in matched:
        source = clean(row.get("source", "")) or "출처 미상"
        key = title_key(row.get("title", ""))
        if not key or key in seen_titles:
            continue
        if source_counts[source] >= 2:
            continue

        selected.append(row)
        seen_titles.add(key)
        source_counts[source] += 1

        if len(selected) >= limit:
            break

    # 출처 제한 때문에 10건 미만이면 남은 고유 자료를 추가합니다.
    if len(selected) < min(10, len(matched)):
        for row in matched:
            key = title_key(row.get("title", ""))
            if not key or key in seen_titles:
                continue
            selected.append(row)
            seen_titles.add(key)
            if len(selected) >= limit:
                break

    return selected


def build_issue_summary(
    topic: str,
    basis_rows: list[dict[str, str]],
) -> dict[str, str]:
    titles = [
        strip_source_suffix(row.get("title", ""), row.get("source", ""))
        for row in basis_rows
        if row.get("title")
    ]

    action_labels = top_signal_labels(titles, ACTION_SIGNALS, 2)
    issue_labels = top_signal_labels(titles, ISSUE_SIGNALS, 2)
    impact_labels = top_signal_labels(titles, IMPACT_SIGNALS, 2)

    if action_labels:
        change = (
            f"최근 관련 자료에서는 {join_labels(action_labels)}가 "
            f"반복적으로 나타났다."
        )
    else:
        change = (
            f"최근 자료는 {topic}의 정책·사업 동향과 사례를 중심으로 다뤘다."
        )

    if issue_labels:
        issue = (
            f"주요 쟁점으로는 {join_labels(issue_labels)}가 함께 확인된다."
        )
    else:
        issue = (
            "제목에서 확인되는 뚜렷한 갈등·비용 쟁점은 제한적이며, "
            "제도와 사업 추진 동향이 중심이다."
        )

    if impact_labels:
        impact = (
            f"도시계획적으로는 {join_labels(impact_labels)}에 미치는 영향을 "
            f"계속 살펴볼 필요가 있다."
        )
    else:
        fallback = TOPIC_IMPACT_DEFAULTS.get(
            topic,
            "토지이용, 생활권과 도시 기능 변화",
        )
        impact = (
            f"도시계획적으로는 {fallback}에 미치는 영향이 주요 관찰 지점이다."
        )

    return {
        "change": change,
        "issue": issue,
        "impact": impact,
    }



def exclusion_reason(category: str, title: str) -> str | None:
    value = clean(title)
    lower = value.lower()

    if category == "연구":
        for pattern in RESEARCH_MEDIA_PATTERNS:
            if re.search(pattern, lower, flags=re.I):
                return "연구 사진·행사 게시물"

    if category == "법령":
        normalized = re.sub(r"[^0-9a-z가-힣]+", "", lower)

        # 법령명이 전혀 없이 일반 메뉴명만 제목으로 잡힌 경우
        if re.fullmatch(
            r"(변경조문|신구조문대비표|개정문|제정개정이유|"
            r"조문정보|법령체계도)(?:시행\d+)?",
            normalized,
        ):
            return "법령명 없는 일반 조문 페이지"

        if re.fullmatch(
            r"(별표|별지|서식)(?:제?\d+(?:의\d+)?)?(?:시행\d+)?",
            normalized,
        ):
            return "법령명 없는 별표·별지 페이지"

        has_generic_phrase = any(
            phrase.lower() in lower
            for phrase in LAW_GENERIC_PHRASES
        )
        has_law_name = any(
            signal.lower() in lower
            for signal in LAW_NAME_SIGNALS
        )
        if has_generic_phrase and not has_law_name:
            return "법령명 없는 일반 조문 페이지"

    return None


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


HTTP = make_session()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_source_suffix(title: str, source: str = "") -> str:
    """Google 뉴스 제목 뒤의 '- 언론사/기관명' 꼬리표를 제거합니다."""
    value = clean(title)

    if source:
        for separator in (" - ", " – ", " — ", " | "):
            suffix = separator + clean(source)
            if value.lower().endswith(suffix.lower()):
                value = value[:-len(suffix)].strip()
                break

    # 기존 archive에는 실제 언론사명이 source 필드에 없는 항목도 있으므로
    # 제목 맨 끝의 짧은 출처 꼬리표를 한 번 더 제거합니다.
    value = re.sub(
        r"\s+(?:-|–|—|\|)\s+[^|–—-]{2,50}$",
        "",
        value,
    ).strip()
    return value


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value))
        return parsed.date() if parsed else None
    except Exception:
        return None


def entry_date(entry: Any) -> date | None:
    for key in ("published", "updated", "dc_date"):
        parsed = parse_date(entry.get(key))
        if parsed:
            return parsed
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return date(value.tm_year, value.tm_mon, value.tm_mday)
            except Exception:
                pass
    return None


def title_key(title: str) -> str:
    value = clean(title).lower()
    value = re.sub(r"\s*[-–—]\s*[^-–—]{2,40}$", "", value)
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def is_relevant(title: str) -> bool:
    lower = title.lower()
    return any(word.lower() in lower for word in RELEVANT_WORDS)


def make_item(
    title: str,
    url: str,
    source: str,
    category: str,
    published: date | None,
) -> dict[str, str] | None:
    title = clean(title)
    url = clean(url)
    if len(title) < 5 or not url or not published:
        return None
    if published < KEEP_START or published > TODAY + timedelta(days=1):
        return None

    reason = exclusion_reason(category, title)
    if reason:
        FILTER_COUNTS[reason] += 1
        return None

    key = f"{published.isoformat()}|{title_key(title)}"
    return {
        "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": source,
        "category": category,
        "date": published.isoformat(),
    }


def google_news(
    query: str,
    category: str,
    source_hint: str,
) -> list[dict[str, str]]:
    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    response = HTTP.get(url, timeout=(12, 35))
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    rows: list[dict[str, str]] = []
    for entry in parsed.entries[:100]:
        raw_title = clean(entry.get("title"))
        published = entry_date(entry)
        source_data = entry.get("source") or {}
        feed_source = (
            clean(source_data.get("title"))
            if isinstance(source_data, dict)
            else ""
        )

        title = strip_source_suffix(raw_title, feed_source)
        if category == "정책" and not is_relevant(title):
            continue

        if category == "정책" and source_hint in {
            "국토교통부", "서울특별시", "경기도"
        }:
            final_source = source_hint
        else:
            final_source = feed_source or source_hint or "Google 뉴스"

        row = make_item(
            title=title,
            url=entry.get("link", ""),
            source=final_source,
            category=category,
            published=published,
        )
        if row:
            rows.append(row)
    return rows


def month_windows() -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    end = TODAY + timedelta(days=1)
    while end > YEAR_START:
        start = max(YEAR_START, end - timedelta(days=31))
        windows.append((start, end))
        end = start
    return windows


def current_jobs() -> list[tuple[str, str, str]]:
    jobs: list[tuple[str, str, str]] = []
    for source, query in OFFICIAL_POLICY_QUERIES:
        jobs.append(("정책", source, f"{query} when:14d"))
    for category, source, query in OTHER_QUERIES:
        jobs.append((category, source, f"{query} when:30d"))
    return jobs


def backfill_jobs() -> list[tuple[str, str, str]]:
    jobs: list[tuple[str, str, str]] = []
    for start, end in month_windows():
        suffix = f" after:{start.isoformat()} before:{end.isoformat()}"
        for source, query in OFFICIAL_POLICY_QUERIES:
            jobs.append(("정책", source, query + suffix))
        for category, source, query in OTHER_QUERIES:
            jobs.append((category, source, query + suffix))
    return jobs


def run_jobs(
    jobs: list[tuple[str, str, str]],
    label: str,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    rows: list[dict[str, str]] = []
    status: dict[str, str] = {}
    source_counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {
            executor.submit(google_news, query, category, source):
            (category, source, query)
            for category, source, query in jobs
        }

        for future in as_completed(future_map):
            category, source, _ = future_map[future]
            try:
                found = future.result()
                rows.extend(found)
                source_counts[source] += len(found)
            except Exception as exc:
                failures[source] += 1
                print(f"[{label}] {source} 실패: {type(exc).__name__}: {exc}")

    for _, source, _ in jobs:
        if source in status:
            continue
        count = source_counts[source]
        failed = failures[source]
        if count:
            status[source] = f"{count}건 수집"
        elif failed:
            status[source] = f"수집 실패 {failed}회"
        else:
            status[source] = "검색 결과 0건"

    return rows, status


def deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    official = {"국토교통부", "서울특별시", "경기도"}

    for original in rows:
        row = dict(original)
        row["title"] = strip_source_suffix(
            row.get("title", ""),
            row.get("source", ""),
        )

        reason = exclusion_reason(
            row.get("category", ""),
            row.get("title", ""),
        )
        if reason:
            FILTER_COUNTS[reason] += 1
            continue

        key = f"{row.get('date', '')}|{title_key(row.get('title', ''))}"
        old = result.get(key)
        if old is None:
            result[key] = row
            continue

        if (
            row.get("source") in official
            and old.get("source") not in official
        ):
            result[key] = row

    return list(result.values())


def row_date(row: dict[str, str]) -> date | None:
    try:
        return date.fromisoformat(row["date"])
    except Exception:
        return None


def period_rows(
    rows: list[dict[str, str]],
    days: int,
) -> list[dict[str, str]]:
    cutoff = TODAY - timedelta(days=days - 1)
    return [
        row for row in rows
        if row_date(row) and row_date(row) >= cutoff
    ]


def category_counts(
    rows: list[dict[str, str]],
    days: int,
) -> dict[str, int]:
    counter = Counter(row["category"] for row in period_rows(rows, days))
    return {
        "정책": counter.get("정책", 0),
        "법령": counter.get("법령", 0),
        "연구": counter.get("연구", 0),
        "기사": counter.get("기사", 0),
    }


def keyword_text(row: dict[str, str]) -> str:
    title = strip_source_suffix(
        row.get("title", ""),
        row.get("source", ""),
    )
    text = title

    # URL 조각과 기관·언론사명을 제거합니다.
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.I)
    source = clean(row.get("source", ""))
    if source:
        text = re.sub(re.escape(source), " ", text, flags=re.I)

    for noise in KEYWORD_NOISE:
        if len(noise) >= 2:
            text = re.sub(re.escape(noise), " ", text, flags=re.I)

    return clean(text)


def keyword_rows(
    rows: list[dict[str, str]],
    days: int,
) -> list[dict[str, Any]]:
    phrase_counter: Counter[str] = Counter()
    fallback_counter: Counter[str] = Counter()

    for row in period_rows(rows, days):
        original = strip_source_suffix(
            row.get("title", ""),
            row.get("source", ""),
        )
        lower = original.lower()

        # 같은 자료에서 동일 키워드는 한 번만 셉니다.
        for label, variants in KEYWORD_PHRASES.items():
            if any(variant.lower() in lower for variant in variants):
                phrase_counter[label] += 1

        clean_text = keyword_text(row)
        source_tokens = {
            token.lower()
            for token in TOKEN_RE.findall(row.get("source", ""))
        }
        tokens = {
            token.lower()
            for token in TOKEN_RE.findall(clean_text)
            if (
                len(token) >= 2
                and token.lower() not in KEYWORD_NOISE
                and token.lower() not in source_tokens
                and not token.isdigit()
            )
        }
        fallback_counter.update(tokens)

    output: list[dict[str, Any]] = []
    used: set[str] = set()

    for word, count in phrase_counter.most_common():
        output.append({"word": word, "count": count})
        used.add(word.lower())
        if len(output) == 20:
            return output

    for word, count in fallback_counter.most_common():
        if word.lower() in used:
            continue
        output.append({"word": word, "count": count})
        used.add(word.lower())
        if len(output) == 20:
            break

    return output


def match_count(title: str, words: tuple[str, ...]) -> int:
    lower = title.lower()
    return sum(1 for word in words if word.lower() in lower)


def issue_rows(
    rows: list[dict[str, str]],
    days: int,
) -> list[dict[str, Any]]:
    current_start = TODAY - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)

    current = [
        row for row in rows
        if row_date(row) and row_date(row) >= current_start
    ]
    previous = [
        row for row in rows
        if row_date(row)
        and previous_start <= row_date(row) < current_start
    ]

    output: list[dict[str, Any]] = []
    for topic, words in TOPICS.items():
        matched = [
            row for row in current
            if match_count(strip_source_suffix(row["title"], row.get("source", "")), words) > 0
        ]
        if not matched:
            continue

        matched.sort(
            key=lambda row: (
                match_count(strip_source_suffix(row["title"], row.get("source", "")), words),
                row["date"],
            ),
            reverse=True,
        )
        previous_count = sum(
            1 for row in previous
            if match_count(strip_source_suffix(row["title"], row.get("source", "")), words) > 0
        )
        difference = len(matched) - previous_count

        if days == 365:
            trend = "최근 1년 누적"
        elif difference > 0:
            trend = f"직전 동일기간보다 {difference}건 증가"
        elif difference < 0:
            trend = f"직전 동일기간보다 {abs(difference)}건 감소"
        else:
            trend = "직전 동일기간과 동일"

        basis_rows = diversify_issue_rows(matched, limit=15)
        summary = build_issue_summary(topic, basis_rows)

        examples: list[dict[str, str]] = []
        for row in basis_rows[:4]:
            examples.append(
                {
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "source": row.get("source", ""),
                    "date": row.get("date", ""),
                }
            )

        output.append(
            {
                "topic": topic,
                "count": len(matched),
                "trend_label": trend,
                "analyzed_count": len(basis_rows),
                "summary": summary,
                "examples": examples,
            }
        )

    output.sort(key=lambda row: row["count"], reverse=True)
    return output[:10]


def coverage(rows: list[dict[str, str]]) -> dict[str, Any]:
    yearly = period_rows(rows, 365)
    dates = [row_date(row) for row in yearly if row_date(row)]

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
    span = (newest - oldest).days + 1

    return {
        "items": len(yearly),
        "oldest": oldest.isoformat(),
        "newest": newest.isoformat(),
        "days_covered": span,
        "complete": (
            len(yearly) >= 150
            and oldest <= TODAY - timedelta(days=330)
        ),
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    old_archive = load_json(ARCHIVE_PATH, [])
    if not isinstance(old_archive, list):
        old_archive = []

    current, current_status = run_jobs(current_jobs(), "최근수집")
    combined = old_archive + current

    old_report = coverage(old_archive)
    state = load_json(STATE_PATH, {})
    need_backfill = (
        not state.get("complete")
        or not old_report.get("complete")
    )

    backfill_status: dict[str, str] = {}
    if need_backfill:
        historical, backfill_status = run_jobs(
            backfill_jobs(),
            "1년 역수집",
        )
        combined.extend(historical)

    archive = deduplicate(combined)
    archive = [
        row for row in archive
        if row_date(row)
        and KEEP_START <= row_date(row) <= TODAY + timedelta(days=1)
    ]
    archive.sort(
        key=lambda row: (row.get("date", ""), row.get("source", "")),
        reverse=True,
    )

    report = coverage(archive)
    updated_at = NOW.strftime("%Y-%m-%d %H:%M KST")
    yearly = period_rows(archive, 365)

    official_recent = [
        row for row in period_rows(archive, 14)
        if row.get("source") in {"국토교통부", "서울특별시", "경기도"}
    ]

    # 세 공식 정책자료가 모두 0건이면 기존 정상 자료를 덮어쓰지 않습니다.
    current_official_count = sum(
        1 for row in current
        if row.get("source") in {"국토교통부", "서울특별시", "경기도"}
    )
    if current_official_count == 0 and not official_recent:
        raise RuntimeError(
            "국토부·서울시·경기도의 최근 공식자료를 한 건도 확인하지 못했습니다."
        )

    if report["complete"]:
        write_json(
            STATE_PATH,
            {
                "complete": True,
                "completed_at": NOW.isoformat(),
                "coverage": report,
            },
        )

    write_json(ARCHIVE_PATH, archive)
    write_json(
        LATEST_PATH,
        {
            "updated_at": updated_at,
            "coverage": report,
            "period_counts": {
                "weekly": category_counts(archive, 7),
                "monthly": category_counts(archive, 30),
                "yearly": category_counts(archive, 365),
            },
            "source_status": current_status,
            "backfill_status": backfill_status,
            "items": yearly[:200],
        },
    )
    write_json(
        KEYWORDS_PATH,
        {
            "updated_at": updated_at,
            "monthly": keyword_rows(archive, 30),
            "quarterly": keyword_rows(archive, 90),
            "yearly": keyword_rows(archive, 365),
        },
    )
    write_json(
        ISSUES_PATH,
        {
            "updated_at": updated_at,
            "coverage": report,
            "weekly": issue_rows(archive, 7),
            "monthly": issue_rows(archive, 30),
            "yearly": issue_rows(archive, 365),
        },
    )

    print("=== 최근 공식자료 ===")
    for source in ("국토교통부", "서울특별시", "경기도"):
        print(f"{source}: {current_status.get(source, '확인 불가')}")

    print("=== 품질 필터 ===")
    if FILTER_COUNTS:
        for reason, count in FILTER_COUNTS.items():
            print(f"{reason}: {count}건 제외")
    else:
        print("제외된 자료 없음")

    print(
        "RESULT "
        f"items={report['items']} "
        f"oldest={report['oldest']} "
        f"newest={report['newest']} "
        f"days={report['days_covered']} "
        f"complete={report['complete']} "
        f"official14d={len(official_recent)}"
    )


if __name__ == "__main__":
    main()
