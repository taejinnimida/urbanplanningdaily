from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from html import unescape
from urllib.parse import quote, urlencode, urljoin

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

# Gemini 무료 API를 이용한 이슈 분석
# 오전 8시는 수집만, 오전 10시와 오후 6시는 변경된 이슈를 한 번에 분석합니다.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
AI_MAX_MATERIALS = 15
AI_MAX_ISSUES = 24
AI_LABELS = ("핵심 변화", "주요 쟁점", "도시계획적 의미")


def should_run_ai_analysis() -> bool:
    value = os.getenv("RUN_AI_ANALYSIS", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    return NOW.hour in {10, 18}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/149 Safari/537.36"
    )
}

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

MUNICIPAL_NOTICE_SOURCES = [
    {"city": "서울", "url": "https://www.seoul.go.kr/news/news_notice.do", "domain": "seoul.go.kr/news/news_notice.do"},
    {"city": "인천", "url": "https://announce.incheon.go.kr/citynet/jsp/sap/SAPGosiBizProcess.do?command=searchList&flag=gosiGL&sido=ic&svp=Y", "domain": "announce.incheon.go.kr"},
    {"city": "수원", "url": "https://www.suwon.go.kr/web/saeallOfr/BD_ofrList.do?q_currPage=1&q_rowPerPage=50", "domain": "suwon.go.kr/web/saeallOfr"},
    {"city": "화성", "url": "https://www.hscity.go.kr/www/gosi/BD_notice.do?q_currPage=1&q_rowPerPage=50", "domain": "hscity.go.kr/www/gosi"},
    {"city": "남양주", "url": "https://www.nyj.go.kr/www/selectEminwonWebList.do?cpn=1&key=2492&sa1Join=01%3B02%3B04%3B05", "domain": "nyj.go.kr/www/selectEminwonWeb"},
    {"city": "고양", "url": "https://eminwon.goyang.go.kr/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do?context=NTIS&countYn=Y&epcCheck=Y&homepage_pbs_yn=Y&initValue=Y&jndinm=OfrNotAncmtEJB&method=selectListOfrNotAncmt&methodnm=selectListOfrNotAncmtHomepage&not_ancmt_se_code=01%2C04%2C05&ofr_pageSize=50&subCheck=Y&title=%EA%B3%A0%EC%8B%9C%EA%B3%B5%EA%B3%A0", "domain": "eminwon.goyang.go.kr"},
    {"city": "성남", "url": "https://www.seongnam.go.kr/notice/publicNotice01.do?menuIdx=1000055&returnURL=%2Fmain.do", "domain": "seongnam.go.kr/notice"},
    {"city": "평택", "url": "https://www.pyeongtaek.go.kr/pyeongtaek/saeol/gosi/list.do?mid=0401020100", "domain": "pyeongtaek.go.kr/pyeongtaek/saeol/gosi"},
    {"city": "과천", "url": "https://www.gccity.go.kr/portal/saeol/gosi/list.do?mId=0301040000", "domain": "gccity.go.kr/portal/saeol/gosi"},
    {"city": "광명", "url": "https://www.gm.go.kr/pt/user/nftcBbs/BD_selectNftcBbsList.do?q_nftcBbsCode=1001", "domain": "gm.go.kr/pt/user/nftcBbs"},
    {"city": "광주", "url": "https://www.gjcity.go.kr/portal/saeol/gosi/list.do?mId=0202010000", "domain": "gjcity.go.kr/portal/saeol/gosi"},
]

URBAN_NOTICE_KEYWORDS = (
    "도시관리계획", "도시계획시설", "도시기본계획", "지구단위계획",
    "정비구역", "정비계획", "정비사업", "재개발", "재건축", "소규모재건축",
    "가로주택정비", "재정비촉진", "도시개발", "개발계획", "산업단지계획",
    "주거환경개선", "공공주택", "역세권", "용도지역", "용도지구", "용도구역",
    "개발행위허가제한", "경관계획", "공원조성계획", "택지개발",
)

MUNICIPAL_NOTICE_DAYS = 7
MUNICIPAL_NOTICE_LIMIT = 28
MUNICIPAL_CITY_LIMIT = 4
MUNICIPAL_NOTICE_EXCLUDE_WORDS = ("지형도면", "실시계획",)
EUM_GOSI_LIST_URL = "https://www.eum.go.kr/web/gs/gv/gvGosiList.jsp"

PUBLIC_MAINTENANCE_SOURCES = [
    {"city": "서울", "query": "(site:seoul.go.kr OR site:cleanup.seoul.go.kr) (신속통합기획 OR 신통기획 OR 공공재개발 OR 공공재건축 OR 모아타운 OR 모아주택 OR 도심복합 OR 공공정비)"},
    {"city": "인천", "query": "(site:incheon.go.kr OR site:ih.co.kr) (공공재개발 OR 공공재건축 OR 공공정비 OR 도심복합 OR 소규모주택정비 OR 정비사업지원)"},
    {"city": "수원", "query": "site:suwon.go.kr (공공재개발 OR 공공재건축 OR 공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅)"},
    {"city": "화성", "query": "site:hscity.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
    {"city": "남양주", "query": "site:nyj.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
    {"city": "고양", "query": "site:goyang.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
    {"city": "성남", "query": "site:seongnam.go.kr (공공재개발 OR 공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅)"},
    {"city": "평택", "query": "site:pyeongtaek.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
    {"city": "과천", "query": "site:gccity.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
    {"city": "광명", "query": "site:gm.go.kr (공공재개발 OR 공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅)"},
    {"city": "광주", "query": "site:gjcity.go.kr (공공정비 OR 정비사업지원 OR 재개발재건축지원 OR 정비사업컨설팅 OR 소규모주택정비)"},
]

PUBLIC_MAINTENANCE_KEYWORDS = (
    "신속통합기획", "신통기획", "공공재개발", "공공재건축", "공공정비",
    "공공참여", "공공지원", "모아타운", "모아주택", "도심복합",
    "정비사업 지원", "정비사업지원", "재개발·재건축 지원", "재개발재건축지원",
    "정비사업 컨설팅", "정비사업컨설팅", "정비사업 지원센터", "정비사업지원센터",
    "소규모주택정비",
)

PUBLIC_MAINTENANCE_DAYS = 7
PUBLIC_MAINTENANCE_LIMIT = 18
PUBLIC_MAINTENANCE_CITY_LIMIT = 3

OTHER_QUERIES = [
    ("법령", "법령·입법", "(site:opinion.lawmaking.go.kr OR site:law.go.kr) (도시 OR 국토 OR 주택 OR 주거 OR 건축 OR 토지 OR 정비 OR 재개발 OR 재건축 OR 공공주택 OR 교통 OR 철도 OR 도시개발 OR 도시재생 OR 공간 OR 지역 OR 농촌) (개정 OR 제정 OR 입법예고 OR 시행령 OR 시행규칙 OR 법률)"),
    ("연구", "연구기관", "(site:krihs.re.kr OR site:si.re.kr OR site:auri.re.kr) (도시 OR 국토 OR 주택 OR 건축 OR 토지 OR 지역 OR 교통) -site:data.si.re.kr/photo"),
    ("기사", "언론", "(도시계획 OR 국토계획 OR 지구단위계획 OR 용도지역 OR 도시개발 OR 도시재생 OR 재개발 OR 재건축 OR 지역소멸 OR 균형발전 OR 공공주택 OR 철도지하화 OR 스마트시티)"),
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

# ── 수정: 장르어 추가 ─────────────────────────────────────────────────
STOPWORDS = {
    "연구", "방안", "위한", "관련", "대한", "통한", "기반", "추진",
    "발표", "개최", "결과", "입법예고", "보도자료", "보고서", "서울시",
    "경기도", "국토교통부", "국토연구원", "서울연구원", "건축공간연구원",
    "정책", "계획", "마련", "강화", "지원", "개선", "확대", "제도",
    "사업", "대응", "활성화", "종합", "새로운", "최근", "전국", "정부",
    "분석", "방향", "현황", "통해", "관한", "기자", "뉴스", "지역",
    "도시", "국토", "올해", "내년", "한국", "대상",
    # 이 사이트 수집 범위 자체인 장르어 → 키워드로서 의미 없음
    "도시계획", "도시개발", "국토계획", "도시정책", "국토정책",
    "도시관리", "도시행정", "토지이용계획",
}

TOPICS = {
    "주택공급·공공주택": ("주택공급", "공공주택", "택지", "임대주택", "주거복지", "분양"),
    "재건축·재개발·정비사업": ("재건축", "재개발", "정비사업", "노후계획도시", "1기 신도시"),
    "지역소멸·균형발전": ("지역소멸", "지방소멸", "균형발전", "생활인구", "인구감소"),
    "철도·광역교통·역세권": ("철도", "GTX", "광역교통", "역세권", "철도지하화", "도시철도"),
    "도시재생·빈집·원도심": ("도시재생", "빈집", "원도심", "구도심", "유휴공간", "공실"),
    "국토계획·용도지역·규제": ("국토계획", "도시계획", "용도지역", "지구단위계획", "개발제한구역", "그린벨트", "용적률"),
    "산업단지·지역산업 전환": ("산업단지", "국가산단", "산업전환", "기업도시", "첨단산업"),
    "스마트시티·AI·디지털전환": ("스마트시티", "스마트도시", "인공지능", "디지털트윈", "자율주행", "도시데이터"),
    "기후위기·탄소중립·녹색건축": ("기후위기", "탄소중립", "녹색건축", "제로에너지", "침수", "폭염", "기후적응"),
    "상권·골목경제·생활권": ("상권", "골목상권", "생활권", "전통시장", "공실", "젠트리피케이션"),
    "농촌공간·농촌재생": ("농촌공간", "농촌재생", "농촌마을", "농촌소멸", "농촌협약"),
    "건축정책·공공건축": ("건축정책", "공공건축", "건축물관리", "노후건축물", "건축안전"),
    "토지·부동산시장": ("부동산", "토지거래", "집값", "지가", "공시가격", "전세", "매매가격"),
    "관광·지역개발": ("관광단지", "지역개발", "관광개발", "문화도시", "복합개발", "워케이션"),
    "도시안전·재난 대응": ("도시안전", "재난", "지진", "산사태", "침수", "화재", "안전진단", "붕괴"),
}

TOKEN_RE = re.compile(r"[가-힣]{2,}")

# ── 수정: 재건축·재개발·정비사업 동의어 통합, 장르어 제거 ──────────────
KEYWORD_PHRASES = {
    # 정비사업 계열 통합 → 재건축+재개발+정비사업 합산
    "정비사업(재건축·재개발)": (
        "재건축", "재개발", "정비사업", "도시정비",
        "소규모재건축", "가로주택정비", "소규모재개발",
    ),
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
    # 도시계획·국토계획·도시개발은 STOPWORDS로 이동 → 여기서 제거
    "토지이용": ("토지이용",),
    "지구단위계획": ("지구단위계획",),
    "용도지역": ("용도지역",),
    "개발제한구역": ("개발제한구역", "그린벨트"),
    "용적률": ("용적률",),
    "공공기여": ("공공기여",),
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

# ── 수정: KEYWORD_NOISE에도 장르어 추가 ──────────────────────────────
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
    # 장르어 추가
    "도시계획", "국토계획", "도시정책", "국토정책", "도시관리", "도시행정",
}

RESEARCH_MEDIA_PATTERNS = (
    r"^\s*\[(?:포토|사진|photo)\]",
    r"^\s*(?:포토\s*뉴스|포토\s*갤러리|포토\s*앨범)\b",
    r"사진으로\s*보는",
    r"(?:옛|과거|예전)\s*(?:사진|모습|풍경)",
    r"(?:사진|기록)\s*아카이브",
    r"(?:기록|역사)\s*사진",
    r"그때\s*그\s*시절",
    r"추억(?:의)?\s*(?:사진|거리|풍경|모습)",
    r"(?:옛|과거|예전).{0,20}(?:길|거리|로|동네|마을).{0,20}(?:사진|모습|풍경)",
    r"(?:길|거리|로|동네|마을).{0,20}(?:옛|과거|예전).{0,20}(?:사진|모습|풍경)",
    r"(?:현장|행사|세미나|포럼)\s*스케치",
    r"(?:행사|현장)\s*사진",
    r"사진\s*(?:공유|모음|자료|갤러리|앨범)",
    r"(?:채용|직원|연구원)\s*(?:공고|모집)",
    r"(?:입찰|용역|계약)\s*(?:공고|안내)",
    r"(?:참가자|교육생|수강생|서포터즈|기자단)\s*모집",
    r"(?:행사|세미나|포럼|설명회|교육)\s*(?:개최\s*)?안내",
    r"(?:참가|수강)\s*(?:신청|접수)",
    r"(?:업무협약|협약식|mou|개소식|방문단|기관방문)",
    r"사진으로\s*(?:만나는|읽는|살펴보는|돌아보는)",
    r"(?:항공사진|거리사진|현장사진|기록사진|옛사진|과거사진|사진자료|사진전|사진집|사진공모전)",
    r"(?:사진|이미지)\s*(?:다운로드|열람|검색|기록|공개)",
    r"(?:19|20)\d{2}\s*년(?:대)?\s*(?:사진|풍경|모습|전경)",
    r"(?:과거와\s*현재|어제와\s*오늘)",
    r"(?:업무추진비|관서업무비|경영공시|정보공개|오시는\s*길|조직도)",
    r"(?:예산서|결산서|수의계약|감사결과|윤리경영|인권경영|회의록)",
    r"(?:카드뉴스|홍보영상|기관동정|수상소식|뉴스레터)",
)


# 제목에 '사진'이 없어도 URL이 사진·갤러리 페이지면 연구자료에서 제외합니다.
RESEARCH_URL_EXCLUDE_PATTERNS = (
    r"data\.si\.re\.kr/photo/",
    r"/photo(?:/|\?|$)",
    r"/gallery(?:/|\?|$)",
    r"/photoView(?:/|\?|$)",
    r"/imageArchive(?:/|\?|$)",
)

LAW_GENERIC_PHRASES = (
    "변경 조문", "변경조문", "신구 조문 대비표", "신구조문대비표",
    "개정문", "제정·개정 이유", "제정개정이유", "조문 정보", "조문정보",
    "법령 체계도", "법령체계도",
)

LAW_NAME_SIGNALS = (
    "법률", "특별법", "기본법", "시행령", "시행규칙", "조례", "규정", "기준", "지침",
)

LAW_RELEVANT_KEYWORDS = (
    "도시", "국토", "주택", "주거", "건축", "토지", "부동산",
    "정비", "재개발", "재건축", "공공주택", "도시개발",
    "도시재생", "빈집", "교통", "철도", "도로", "주차",
    "공간", "지역", "농촌", "산업단지", "물류", "경관",
    "공원", "녹지", "기반시설", "용도지역", "지구단위",
    "택지", "역세권", "생활권", "기후", "탄소", "환경",
)

LAW_GENERIC_TITLE_PATTERNS = (
    r"^\s*(?:관련\s*)?법령\s*$",
    r"^\s*(?:관련\s*)?법령\s*(?:개정|제정|변경|안내)\s*$",
    r"^\s*(?:법률|시행령|시행규칙)\s*(?:개정|제정|변경)\s*$",
    r"^\s*(?:일부|전부)?개정(?:안|령안)?\s*$",
    r"^\s*(?:명칭|제명)\s*(?:변경|개칭)\s*$",
)

LAW_RENAME_PATTERNS = (
    r"(?:법령|법률|조례|규정).{0,12}(?:명칭|제명).{0,8}(?:변경|개칭)",
    r"(?:명칭|제명).{0,8}(?:변경|개칭)",
)

FILTER_COUNTS: Counter[str] = Counter()

FLOW_DIMENSIONS = {
    "정책·제도": {
        "개정": "법령 개정", "시행": "제도 시행", "완화": "규제 완화",
        "강화": "관리 강화", "도입": "새 제도 도입", "지정": "구역·사업 지정",
        "지원": "지원 확대", "공모": "사업 공모", "승인": "계획·사업 승인",
    },
    "사업·공급": {
        "추진": "사업 추진", "공급": "공급 확대", "착공": "사업 착공",
        "준공": "사업 준공", "조성": "공간·시설 조성", "정비": "정비 추진",
        "유치": "기능·기업 유치", "계획": "계획 수립",
    },
    "시장·비용": {
        "사업성": "사업성", "공사비": "공사비", "분담금": "분담금",
        "미분양": "미분양", "가격": "가격 변동", "거래": "거래 변화",
        "지가": "지가 변화", "임대료": "임대료", "부담": "비용 부담",
    },
    "갈등·리스크": {
        "갈등": "이해관계자 갈등", "반발": "주민 반발", "반대": "반대 여론",
        "소송": "법적 분쟁", "지연": "사업 지연", "취소": "사업 취소",
        "무산": "사업 무산", "논란": "정책·사업 논란",
    },
    "공간·생활권": {
        "역세권": "역세권 재편", "상권": "상권 변화", "생활권": "생활권 변화",
        "빈집": "빈집 관리", "인구": "인구 변화", "소멸": "지역소멸",
        "교통": "교통체계 변화", "철도": "철도축 변화",
        "용적률": "개발밀도 변화", "산업단지": "산업입지 변화",
    },
    "안전·환경": {
        "침수": "침수 대응", "폭염": "폭염 대응", "기후": "기후위기 대응",
        "탄소": "탄소중립", "안전": "안전관리", "재난": "재난 대응", "녹색": "녹색건축",
    },
}

FLOW_SENTENCE = {
    "정책·제도": "{labels} 관련 움직임이 {titles}건의 자료에서 반복됐다.",
    "사업·공급": "{labels} 흐름이 {titles}건의 자료에서 확인됐다.",
    "시장·비용": "{labels} 문제가 {titles}건의 자료에서 주요 관심사로 나타났다.",
    "갈등·리스크": "{labels} 위험이 {titles}건의 자료에서 반복적으로 제기됐다.",
    "공간·생활권": "{labels}가 {titles}건의 자료에서 공간 변화의 핵심으로 나타났다.",
    "안전·환경": "{labels}이 {titles}건의 자료에서 주요 대응 과제로 나타났다.",
}


def diversify_issue_rows(matched, limit=15):
    selected = []
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


def build_issue_summary(topic, basis_rows):
    titles = [
        strip_source_suffix(row.get("title", ""), row.get("source", ""))
        for row in basis_rows if row.get("title")
    ]
    points = []
    for dimension, signals in FLOW_DIMENSIONS.items():
        label_counts: Counter[str] = Counter()
        matched_title_count = 0
        for title in titles:
            lower = title.lower()
            labels_in_title: set[str] = set()
            for signal, label in signals.items():
                if signal.lower() in lower:
                    labels_in_title.add(label)
            if labels_in_title:
                matched_title_count += 1
                label_counts.update(labels_in_title)
        if matched_title_count < 2:
            continue
        top_labels = [label for label, _ in label_counts.most_common(3)]
        if not top_labels:
            continue
        labels_text = "·".join(top_labels)
        sentence = FLOW_SENTENCE[dimension].format(labels=labels_text, titles=matched_title_count)
        points.append({"label": dimension, "text": sentence, "evidence_count": matched_title_count})
    points.sort(key=lambda p: p["evidence_count"], reverse=True)
    points = points[:3]
    note = "" if points else "관련 제목들 사이에서 2건 이상 반복된 공통 흐름이 뚜렷하지 않아 대표 자료만 제시합니다."
    return {"points": points, "note": note, "topic": topic}


def exclusion_reason(category, title, url="", source=""):
    value = clean(title)
    lower = value.lower()
    url_lower = clean(url).lower()

    if category == "연구":
        for pattern in RESEARCH_URL_EXCLUDE_PATTERNS:
            if re.search(pattern, url_lower, flags=re.I):
                return "연구 사진·갤러리 페이지"

        for pattern in RESEARCH_MEDIA_PATTERNS:
            if re.search(pattern, lower, flags=re.I):
                return "연구 사진·홍보·행정 게시물"

        if "서울연구데이터서비스" in source and "/photo/" in url_lower:
            return "연구 사진·갤러리 페이지"

    if category == "법령":
        normalized = re.sub(r"[^0-9a-z가-힣]+", "", lower)
        if not any(keyword.lower() in lower for keyword in LAW_RELEVANT_KEYWORDS):
            return "도시계획 관련 키워드 없는 법령"
        for pattern in LAW_GENERIC_TITLE_PATTERNS:
            if re.fullmatch(pattern, lower, flags=re.I):
                return "일반적인 법령 개정 제목"
        for pattern in LAW_RENAME_PATTERNS:
            if re.search(pattern, lower, flags=re.I):
                return "단순 명칭 변경 법령"
        if re.fullmatch(
            r"(변경조문|신구조문대비표|개정문|제정개정이유|조문정보|법령체계도)(?:시행\d+)?",
            normalized,
        ):
            return "법령명 없는 일반 조문 페이지"
        if re.fullmatch(
            r"(별표|별지|서식)(?:제?\d+(?:의\d+)?)?(?:시행\d+)?",
            normalized,
        ):
            return "법령명 없는 별표·별지 페이지"
        has_generic_phrase = any(
            phrase.lower() in lower for phrase in LAW_GENERIC_PHRASES
        )
        has_law_name = any(
            signal.lower() in lower for signal in LAW_NAME_SIGNALS
        )
        if has_generic_phrase and not has_law_name:
            return "법령명 없는 일반 조문 페이지"
    return None


def make_session():
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


HTTP = make_session()


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_source_suffix(title, source=""):
    value = clean(title)
    if source:
        for separator in (" - ", " – ", " — ", " | "):
            suffix = separator + clean(source)
            if value.lower().endswith(suffix.lower()):
                value = value[:-len(suffix)].strip()
                break
    value = re.sub(r"\s+(?:-|–|—|\|)\s+[^|–—-]{2,50}$", "", value).strip()
    return value


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_date(value):
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value))
        return parsed.date() if parsed else None
    except Exception:
        return None


def entry_date(entry):
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


def title_key(title):
    value = clean(title).lower()
    value = re.sub(r"\s*[-–—]\s*[^-–—]{2,40}$", "", value)
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def is_relevant(title):
    lower = title.lower()
    return any(word.lower() in lower for word in RELEVANT_WORDS)


def make_item(
    title,
    url,
    source,
    category,
    published,
    description="",
):
    title = clean(title)
    url = clean(url)
    description = clean(description)[:700]
    if len(title) < 5 or not url or not published:
        return None
    if published < KEEP_START or published > TODAY + timedelta(days=1):
        return None
    reason = exclusion_reason(category, title, url, source)
    if reason:
        FILTER_COUNTS[reason] += 1
        return None
    key = f"{published.isoformat()}|{title_key(title)}"
    row = {
        "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": source,
        "category": category,
        "date": published.isoformat(),
    }
    if description and description.lower() != title.lower():
        row["description"] = description
    return row


def strip_html(value):
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return clean(unescape(value))


def parse_notice_date(value):
    match = re.search(r"(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def is_urban_notice(title):
    lower = clean(title).lower()
    if any(word.lower() in lower for word in MUNICIPAL_NOTICE_EXCLUDE_WORDS):
        return False
    return any(keyword.lower() in lower for keyword in URBAN_NOTICE_KEYWORDS)


def make_eum_gosi_url(title, published):
    params = {
        "startdt": (published - timedelta(days=2)).isoformat(),
        "enddt": (published + timedelta(days=2)).isoformat(),
        "zonenm": clean(title), "pageNo": "1",
    }
    return EUM_GOSI_LIST_URL + "?" + urlencode(params)


def extract_notice_rows(html_text, source):
    cutoff = TODAY - timedelta(days=MUNICIPAL_NOTICE_DAYS - 1)
    blocks = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", html_text)
    blocks += re.findall(r"(?is)<li\b[^>]*>.*?</li>", html_text)
    rows = []
    seen: set[str] = set()
    for block in blocks:
        block_text = strip_html(block)
        published = parse_notice_date(block_text)
        if not published or published < cutoff or published > TODAY + timedelta(days=1):
            continue
        anchors = re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block)
        candidates = []
        for href, label_html in anchors:
            title = strip_html(label_html)
            if len(title) < 6 or not is_urban_notice(title):
                continue
            candidates.append((title, href))
        if not candidates:
            continue
        title, href = max(candidates, key=lambda item: len(item[0]))
        key = f"{source['city']}|{published.isoformat()}|{title_key(title)}"
        if key in seen:
            continue
        seen.add(key)
        eum_url = make_eum_gosi_url(title, published)
        if href.lower().startswith("javascript:") or href.startswith("#"):
            link = eum_url
            source_type = "토지이음 확인"
        else:
            link = urljoin(source["url"], href)
            source_type = "공식 고시공고"
        rows.append({"city": source["city"], "title": title, "url": link,
                     "eum_url": eum_url, "date": published.isoformat(), "source_type": source_type})
    return rows


def fallback_municipal_notices(source):
    terms = ("(도시관리계획 OR 도시계획시설 OR 지구단위계획 OR 정비구역 "
             "OR 재개발 OR 재건축 OR 도시개발 OR 정비계획 OR 용도지역)")
    query = f"site:{source['domain']} {terms} when:{MUNICIPAL_NOTICE_DAYS}d"
    url = "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    response = HTTP.get(url, timeout=(12, 35))
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    cutoff = TODAY - timedelta(days=MUNICIPAL_NOTICE_DAYS - 1)
    rows = []
    seen: set[str] = set()
    for entry in parsed.entries[:60]:
        source_data = entry.get("source") or {}
        feed_source = clean(source_data.get("title")) if isinstance(source_data, dict) else ""
        title = strip_source_suffix(clean(entry.get("title")), feed_source)
        published = entry_date(entry)
        if not published or published < cutoff or not is_urban_notice(title):
            continue
        key = f"{source['city']}|{published.isoformat()}|{title_key(title)}"
        if key in seen:
            continue
        seen.add(key)
        rows.append({"city": source["city"], "title": title,
                     "url": clean(entry.get("link", "")) or source["url"],
                     "eum_url": make_eum_gosi_url(title, published),
                     "date": published.isoformat(), "source_type": "공식 누리집 검색"})
    return rows


def collect_one_municipal_source(source):
    direct_error = ""
    try:
        response = HTTP.get(source["url"], timeout=(12, 35), allow_redirects=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        rows = extract_notice_rows(response.text, source)
        if rows:
            return rows, f"공식 목록 {len(rows)}건"
        direct_error = "공식 목록 0건"
    except Exception as exc:
        direct_error = f"공식 목록 {type(exc).__name__}"
    try:
        fallback = fallback_municipal_notices(source)
        if fallback:
            return fallback, f"{direct_error}, 검색보완 {len(fallback)}건"
        return [], f"{direct_error}, 검색보완 0건"
    except Exception as exc:
        return [], f"{direct_error}, 검색보완 {type(exc).__name__}"


def collect_municipal_notices():
    all_rows = []
    status = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(collect_one_municipal_source, source): source
                      for source in MUNICIPAL_NOTICE_SOURCES}
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                rows, message = future.result()
                all_rows.extend(rows)
                status[source["city"]] = message
            except Exception as exc:
                status[source["city"]] = f"수집 실패: {type(exc).__name__}"
    deduped = {}
    for row in all_rows:
        key = f"{row['city']}|{row['date']}|{title_key(row['title'])}"
        deduped[key] = row
    sorted_rows = sorted(deduped.values(), key=lambda r: (r["date"], r["city"]), reverse=True)
    selected = []
    city_counts: Counter[str] = Counter()
    for row in sorted_rows:
        if city_counts[row["city"]] >= MUNICIPAL_CITY_LIMIT:
            continue
        selected.append(row)
        city_counts[row["city"]] += 1
        if len(selected) >= MUNICIPAL_NOTICE_LIMIT:
            break
    return selected, status


def google_news(query, category, source_hint):
    url = "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    response = HTTP.get(url, timeout=(12, 35))
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    rows = []
    for entry in parsed.entries[:100]:
        raw_title = clean(entry.get("title"))
        published = entry_date(entry)
        source_data = entry.get("source") or {}
        feed_source = clean(source_data.get("title")) if isinstance(source_data, dict) else ""
        title = strip_source_suffix(raw_title, feed_source)
        if category == "정책" and not is_relevant(title):
            continue
        if category == "정책" and source_hint in {"국토교통부", "서울특별시", "경기도"}:
            final_source = source_hint
        else:
            final_source = feed_source or source_hint or "Google 뉴스"
        description = strip_html(
            entry.get("summary")
            or entry.get("description")
            or ""
        )
        row = make_item(
            title=title,
            url=entry.get("link", ""),
            source=final_source,
            category=category,
            published=published,
            description=description,
        )
        if row:
            rows.append(row)
    return rows


def collect_public_maintenance_updates():
    rows = []
    status = {}
    cutoff = TODAY - timedelta(days=PUBLIC_MAINTENANCE_DAYS - 1)

    def collect_one(source):
        city = source["city"]
        query = source["query"] + f" when:{PUBLIC_MAINTENANCE_DAYS}d"
        found = google_news(query, "정비", city)
        selected = []
        for row in found:
            title = clean(row.get("title", ""))
            published = row_date(row)
            if not published or published < cutoff:
                continue
            if not any(keyword.lower() in title.lower() for keyword in PUBLIC_MAINTENANCE_KEYWORDS):
                continue
            copied = dict(row)
            copied["city"] = city
            copied["source"] = city
            copied["source_type"] = "공식 정비사업 소식"
            selected.append(copied)
        return city, selected

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(collect_one, source): source for source in PUBLIC_MAINTENANCE_SOURCES}
        for future in as_completed(futures):
            source = futures[future]
            city = source["city"]
            try:
                _, found = future.result()
                rows.extend(found)
                status[city] = f"{len(found)}건"
            except Exception as exc:
                status[city] = f"수집 실패: {type(exc).__name__}"

    unique = {}
    for row in rows:
        key = f"{row.get('city')}|{row.get('date')}|{title_key(row.get('title', ''))}"
        unique[key] = row
    ordered = sorted(unique.values(), key=lambda r: (r.get("date", ""), r.get("city", "")), reverse=True)
    output = []
    city_counts: Counter[str] = Counter()
    for row in ordered:
        city = row.get("city", "")
        if city_counts[city] >= PUBLIC_MAINTENANCE_CITY_LIMIT:
            continue
        output.append(row)
        city_counts[city] += 1
        if len(output) >= PUBLIC_MAINTENANCE_LIMIT:
            break
    return output, status


def month_windows():
    windows = []
    end = TODAY + timedelta(days=1)
    while end > YEAR_START:
        start = max(YEAR_START, end - timedelta(days=31))
        windows.append((start, end))
        end = start
    return windows


def current_jobs():
    jobs = []
    for source, query in OFFICIAL_POLICY_QUERIES:
        jobs.append(("정책", source, f"{query} when:14d"))
    for category, source, query in OTHER_QUERIES:
        jobs.append((category, source, f"{query} when:30d"))
    return jobs


def backfill_jobs():
    jobs = []
    for start, end in month_windows():
        suffix = f" after:{start.isoformat()} before:{end.isoformat()}"
        for source, query in OFFICIAL_POLICY_QUERIES:
            jobs.append(("정책", source, query + suffix))
        for category, source, query in OTHER_QUERIES:
            jobs.append((category, source, query + suffix))
    return jobs


def run_jobs(jobs, label):
    rows = []
    status = {}
    source_counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(google_news, query, category, source): (category, source, query)
                      for category, source, query in jobs}
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


def deduplicate(rows):
    result = {}
    official = {"국토교통부", "서울특별시", "경기도"}
    for original in rows:
        row = dict(original)
        row["title"] = strip_source_suffix(row.get("title", ""), row.get("source", ""))
        reason = exclusion_reason(
            row.get("category", ""),
            row.get("title", ""),
            row.get("url", ""),
            row.get("source", ""),
        )
        if reason:
            FILTER_COUNTS[reason] += 1
            continue
        key = f"{row.get('date', '')}|{title_key(row.get('title', ''))}"
        old = result.get(key)
        if old is None:
            result[key] = row
            continue
        if row.get("source") in official and old.get("source") not in official:
            result[key] = row
    return list(result.values())


def row_date(row):
    try:
        return date.fromisoformat(row["date"])
    except Exception:
        return None


def period_rows(rows, days):
    cutoff = TODAY - timedelta(days=days - 1)
    return [row for row in rows if row_date(row) and row_date(row) >= cutoff]


def category_counts(rows, days):
    counter = Counter(row["category"] for row in period_rows(rows, days))
    return {"정책": counter.get("정책", 0), "법령": counter.get("법령", 0),
            "연구": counter.get("연구", 0), "기사": counter.get("기사", 0)}


def keyword_text(row):
    title = strip_source_suffix(row.get("title", ""), row.get("source", ""))
    text = title
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.I)
    source = clean(row.get("source", ""))
    if source:
        text = re.sub(re.escape(source), " ", text, flags=re.I)
    for noise in KEYWORD_NOISE:
        if len(noise) >= 2:
            text = re.sub(re.escape(noise), " ", text, flags=re.I)
    return clean(text)


def keyword_rows(rows, days):
    phrase_counter: Counter[str] = Counter()
    fallback_counter: Counter[str] = Counter()
    for row in period_rows(rows, days):
        original = strip_source_suffix(row.get("title", ""), row.get("source", ""))
        lower = original.lower()
        for label, variants in KEYWORD_PHRASES.items():
            if any(variant.lower() in lower for variant in variants):
                phrase_counter[label] += 1
        clean_text = keyword_text(row)
        source_tokens = {token.lower() for token in TOKEN_RE.findall(row.get("source", ""))}
        tokens = {
            token.lower() for token in TOKEN_RE.findall(clean_text)
            if (len(token) >= 2 and token.lower() not in KEYWORD_NOISE
                and token.lower() not in source_tokens and not token.isdigit())
        }
        fallback_counter.update(tokens)
    output = []
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


def match_count(title, words):
    lower = title.lower()
    return sum(1 for word in words if word.lower() in lower)



def issue_analysis_materials(basis_rows):
    materials = []
    for row in basis_rows[:AI_MAX_MATERIALS]:
        item = {
            "title": strip_source_suffix(
                row.get("title", ""),
                row.get("source", ""),
            ),
            "source": clean(row.get("source", "")),
            "date": clean(row.get("date", "")),
        }
        description = clean(row.get("description", ""))
        if description:
            item["description"] = description[:500]
        materials.append(item)
    return materials


def issue_fingerprint(topic, materials):
    payload = {
        "topic": topic,
        "materials": materials,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def reuse_previous_ai_summaries(issue_payload, previous_payload):
    reused = 0
    for period_key in ("weekly", "monthly", "yearly"):
        previous_rows = {
            row.get("topic"): row
            for row in previous_payload.get(period_key, [])
            if isinstance(row, dict)
        }
        for row in issue_payload.get(period_key, []):
            previous = previous_rows.get(row.get("topic"))
            if not previous:
                continue
            if (
                previous.get("analysis_fingerprint")
                != row.get("analysis_fingerprint")
            ):
                continue
            summary = previous.get("summary") or {}
            if summary.get("mode") != "ai":
                continue
            row["summary"] = summary
            reused += 1
    return reused


def ai_response_schema():
    return {
        "type": "object",
        "properties": {
            "analyses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "points": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "enum": list(AI_LABELS),
                                    },
                                    "text": {
                                        "type": "string",
                                        "description": (
                                            "자료의 공통 흐름을 종합한 "
                                            "한국어 1문장"
                                        ),
                                    },
                                },
                                "required": ["label", "text"],
                            },
                        },
                        "note": {"type": "string"},
                    },
                    "required": ["id", "points", "note"],
                },
            }
        },
        "required": ["analyses"],
    }


def extract_gemini_json(response_data):
    if isinstance(response_data, dict) and "analyses" in response_data:
        return response_data

    candidates = response_data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini 응답에 candidates가 없습니다.")

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )
    text = "".join(
        clean(part.get("text", ""))
        for part in parts
        if isinstance(part, dict)
    )
    if not text:
        raise ValueError("Gemini 응답 본문이 비어 있습니다.")

    text = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", text.strip())
    return json.loads(text)


def call_gemini_issue_analysis(candidates):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY가 등록되지 않았습니다.")

    request_rows = []
    id_map = {}

    for period_key, row in candidates[:AI_MAX_ISSUES]:
        issue_id = (
            f"{period_key}:"
            f"{row.get('analysis_fingerprint', '')}"
        )
        id_map[issue_id] = row
        request_rows.append(
            {
                "id": issue_id,
                "period": period_key,
                "topic": row.get("topic", ""),
                "count": row.get("count", 0),
                "materials": row.get("_analysis_materials", []),
            }
        )

    prompt = (
        "당신은 20년 경력 도시계획 실무자를 돕는 전문 편집자다.\\n"
        "아래 이슈별 공개자료 제목·검색 설명문·출처·날짜를 종합해 "
        "각 이슈를 분석하라.\\n\\n"
        "작성 원칙:\\n"
        "1. 개별 제목을 순서대로 풀어쓰거나 나열하지 않는다.\\n"
        "2. 여러 자료에 공통으로 나타난 변화와 쟁점을 종합한다.\\n"
        "3. 같은 사건의 반복보도는 하나의 흐름으로 취급한다.\\n"
        "4. 자료에 없는 수치·원인·전망·정책효과를 만들지 않는다.\\n"
        "5. 2~3개 항목만 작성한다. 근거가 약한 항목은 생략한다.\\n"
        "6. 각 문장은 45~110자 정도의 자연스러운 한국어로 쓴다.\\n"
        "7. '도시계획적 의미'는 공간구조, 생활권, 사업성, "
        "공공성, 형평성, 도시관리 중 자료로 뒷받침되는 내용만 쓴다.\\n"
        "8. 공통 흐름이 약하면 note에 그 한계를 짧게 적는다.\\n\\n"
        "분석할 이슈 JSON:\\n"
        + json.dumps(request_rows, ensure_ascii=False)
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
            "responseFormat": {
                "text": {
                    "mimeType": "application/json",
                    "schema": ai_response_schema(),
                }
            },
        },
    }

    response = HTTP.post(
        GEMINI_API_URL,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(15, 150),
    )
    response.raise_for_status()
    parsed = extract_gemini_json(response.json())

    applied = 0
    for analysis in parsed.get("analyses", []):
        row = id_map.get(clean(analysis.get("id", "")))
        if not row:
            continue

        points = []
        used_labels = set()
        for point in analysis.get("points", []):
            label = clean(point.get("label", ""))
            text = clean(point.get("text", ""))
            if (
                label not in AI_LABELS
                or label in used_labels
                or len(text) < 15
            ):
                continue
            used_labels.add(label)
            points.append({"label": label, "text": text})

        if len(points) < 2:
            continue

        row["summary"] = {
            "mode": "ai",
            "model": GEMINI_MODEL,
            "generated_at": NOW.isoformat(),
            "points": points[:3],
            "note": clean(analysis.get("note", "")),
        }
        applied += 1

    return applied


def enrich_issue_payload_with_ai(issue_payload, previous_payload):
    reused = reuse_previous_ai_summaries(
        issue_payload,
        previous_payload,
    )

    candidates = []
    for period_key in ("weekly", "monthly", "yearly"):
        for row in issue_payload.get(period_key, []):
            summary = row.get("summary") or {}
            if summary.get("mode") != "ai":
                candidates.append((period_key, row))

    applied = 0
    error = ""

    if should_run_ai_analysis() and candidates:
        try:
            applied = call_gemini_issue_analysis(candidates)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"[Gemini AI 분석] 실패, 규칙형 유지: {error}")

    for period_key in ("weekly", "monthly", "yearly"):
        for row in issue_payload.get(period_key, []):
            row.pop("_analysis_materials", None)

    return {
        "requested": should_run_ai_analysis(),
        "key_configured": bool(
            os.getenv("GEMINI_API_KEY", "").strip()
        ),
        "model": GEMINI_MODEL,
        "reused": reused,
        "updated": applied,
        "fallback": sum(
            1
            for period_key in ("weekly", "monthly", "yearly")
            for row in issue_payload.get(period_key, [])
            if (row.get("summary") or {}).get("mode") != "ai"
        ),
        "error": error,
    }

def issue_rows(rows, days):
    current_start = TODAY - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    current = [row for row in rows if row_date(row) and row_date(row) >= current_start]
    previous = [row for row in rows if row_date(row) and previous_start <= row_date(row) < current_start]
    output = []
    for topic, words in TOPICS.items():
        matched = [row for row in current
                   if match_count(strip_source_suffix(row["title"], row.get("source", "")), words) > 0]
        if not matched:
            continue
        matched.sort(key=lambda r: (match_count(strip_source_suffix(r["title"], r.get("source", "")), words), r["date"]), reverse=True)
        previous_count = sum(1 for row in previous
                             if match_count(strip_source_suffix(row["title"], row.get("source", "")), words) > 0)
        difference = len(matched) - previous_count
        if days == 365:
            trend = "최근 1년 누적"
        elif difference > 0:
            trend = f"직전 동일기간보다 {difference}건 증가"
        elif difference < 0:
            trend = f"직전 동일기간보다 {abs(difference)}건 감소"
        else:
            trend = "직전 동일기간과 동일"
        basis_rows = diversify_issue_rows(
            matched,
            limit=AI_MAX_MATERIALS,
        )
        materials = issue_analysis_materials(basis_rows)
        summary = build_issue_summary(topic, basis_rows)
        summary["mode"] = "rule"
        examples = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source": r.get("source", ""),
                "date": r.get("date", ""),
            }
            for r in basis_rows[:4]
        ]
        output.append(
            {
                "topic": topic,
                "count": len(matched),
                "trend_label": trend,
                "analyzed_count": len(basis_rows),
                "analysis_fingerprint": issue_fingerprint(
                    topic,
                    materials,
                ),
                "_analysis_materials": materials,
                "summary": summary,
                "examples": examples,
            }
        )
    output.sort(key=lambda r: r["count"], reverse=True)
    return output[:8]


def coverage(rows):
    yearly = period_rows(rows, 365)
    dates = [row_date(row) for row in yearly if row_date(row)]
    if not dates:
        return {"items": 0, "oldest": None, "newest": None, "days_covered": 0, "complete": False}
    oldest = min(dates)
    newest = max(dates)
    span = (newest - oldest).days + 1
    return {"items": len(yearly), "oldest": oldest.isoformat(), "newest": newest.isoformat(),
            "days_covered": span, "complete": (len(yearly) >= 150 and oldest <= TODAY - timedelta(days=330))}


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    old_archive = load_json(ARCHIVE_PATH, [])
    if not isinstance(old_archive, list):
        old_archive = []
    current, current_status = run_jobs(current_jobs(), "최근수집")
    municipal_notices, municipal_status = collect_municipal_notices()
    maintenance_updates, maintenance_status = collect_public_maintenance_updates()
    combined = old_archive + current
    old_report = coverage(old_archive)
    state = load_json(STATE_PATH, {})
    need_backfill = not state.get("complete") or not old_report.get("complete")
    backfill_status = {}
    if need_backfill:
        historical, backfill_status = run_jobs(backfill_jobs(), "1년 역수집")
        combined.extend(historical)
    archive = deduplicate(combined)
    quality_archive = []
    for row in archive:
        category = clean(row.get("category", ""))
        title = clean(row.get("title", ""))
        reason = exclusion_reason(
            category,
            title,
            row.get("url", ""),
            row.get("source", ""),
        )
        if reason:
            FILTER_COUNTS["기존자료 정리: " + reason] += 1
            continue
        quality_archive.append(row)
    archive = [row for row in quality_archive
               if row_date(row) and KEEP_START <= row_date(row) <= TODAY + timedelta(days=1)]
    archive.sort(key=lambda r: (r.get("date", ""), r.get("source", "")), reverse=True)
    report = coverage(archive)
    updated_at = NOW.strftime("%Y-%m-%d %H:%M KST")
    yearly = period_rows(archive, 365)
    official_recent = [row for row in period_rows(archive, 14)
                       if row.get("source") in {"국토교통부", "서울특별시", "경기도"}]
    current_official_count = sum(1 for row in current
                                 if row.get("source") in {"국토교통부", "서울특별시", "경기도"})
    if current_official_count == 0 and not official_recent:
        raise RuntimeError("국토부·서울시·경기도의 최근 공식자료를 한 건도 확인하지 못했습니다.")
    if report["complete"]:
        write_json(STATE_PATH, {"complete": True, "completed_at": NOW.isoformat(), "coverage": report})
    write_json(ARCHIVE_PATH, archive)
    write_json(LATEST_PATH, {
        "updated_at": updated_at, "coverage": report,
        "period_counts": {
            "weekly": category_counts(archive, 7),
            "monthly": category_counts(archive, 30),
            "yearly": category_counts(archive, 365),
        },
        "source_status": current_status, "backfill_status": backfill_status,
        "municipal_notices": municipal_notices, "municipal_status": municipal_status,
        "maintenance_updates": maintenance_updates, "maintenance_status": maintenance_status,
        "items": yearly[:200],
    })
    write_json(KEYWORDS_PATH, {
        "updated_at": updated_at,
        "monthly": keyword_rows(archive, 30),
        "quarterly": keyword_rows(archive, 90),
        "yearly": keyword_rows(archive, 365),
    })
    previous_issues = load_json(ISSUES_PATH, {})
    if not isinstance(previous_issues, dict):
        previous_issues = {}

    issue_payload = {
        "weekly": issue_rows(archive, 7),
        "monthly": issue_rows(archive, 30),
        "yearly": issue_rows(archive, 365),
    }
    ai_status = enrich_issue_payload_with_ai(
        issue_payload,
        previous_issues,
    )

    write_json(
        ISSUES_PATH,
        {
            "updated_at": updated_at,
            "coverage": report,
            "ai_status": ai_status,
            **issue_payload,
        },
    )
    print("=== Gemini AI 이슈 분석 ===")
    print(
        f"요청={ai_status['requested']} "
        f"키등록={ai_status['key_configured']} "
        f"재사용={ai_status['reused']} "
        f"신규={ai_status['updated']} "
        f"규칙형={ai_status['fallback']}"
    )
    if ai_status["error"]:
        print(f"오류={ai_status['error']}")

    print("=== 최근 공식자료 ===")
    for source in ("국토교통부", "서울특별시", "경기도"):
        print(f"{source}: {current_status.get(source, '확인 불가')}")
    print(f"=== 공공지원 정비사업 추진사항 ===\n최근 추진사항 {len(maintenance_updates)}건")
    print(f"=== 주요 도시계획 고시 ===\n선택 도시 고시 {len(municipal_notices)}건")
    print("=== 품질 필터 ===")
    if FILTER_COUNTS:
        for reason, count in FILTER_COUNTS.items():
            print(f"{reason}: {count}건 제외")
    else:
        print("제외된 자료 없음")
    print(f"RESULT items={report['items']} oldest={report['oldest']} newest={report['newest']} "
          f"days={report['days_covered']} complete={report['complete']} official14d={len(official_recent)}")


if __name__ == "__main__":
    main()
