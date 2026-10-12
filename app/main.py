"""
화학물질 유해성 중계 서버 (Synology DS218+ / Container Manager 용)
------------------------------------------------------------------
역할
  1) 브라우저가 직접 못 부르는 공개 DB(PubChem 등)를 서버가 대신 조회 → CORS 우회
  2) 조회 결과를 하나의 JSON으로 정리해서 프런트(콘솔)에 반환
  3) 디스크 캐시로 반복 조회를 빠르게 + PubChem 호출량 절감
  4) 국내 정부 DB(기후에너지환경부/KOSHA 등) 스크래핑을 붙일 수 있는 자리 마련(fetch_korea_*)

이 서버는 UI(정적 콘솔)와 API를 동시에 제공합니다.
  - UI  :  GET /            → app/static/index.html
  - API :  GET /api/chem?q=<CAS 또는 물질명>
"""
from fastapi import FastAPI, Query, Depends, HTTPException, status, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import httpx, json, re, hashlib, time, pathlib, asyncio, os, secrets

from . import generic_review, generic_rules

# ── Gemini(Google) 설정 ─────────────────────────────────────────────
# Google AI Studio 무료 API 키를 서버 환경변수로 주입 → 브라우저에 노출 안 됨.
# 무료 티어(카드 불필요)로 "자세히 생성"을 돌립니다.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/models"
# 총칭명 검토는 구조식 판독·명명법 판단이 필요해 더 강한 모델(pro 계열)을 따로 지정할 수 있다.
GENERIC_REVIEW_MODEL = os.getenv("GENERIC_REVIEW_MODEL", "") or GEMINI_MODEL

# ── CAS Common Chemistry API 설정 ──────────────────────────────────
# 참고자료 우선순위: 1) CAS Common Chemistry  2) PubChem
# https://www.cas.org/services/commonchemistry-api 에서 API 키를 발급받아
# CAS_CC_API_KEY 환경변수로 주입하면 자동으로 1순위 출처가 됩니다.
# 키가 없으면 자동으로 PubChem(2순위)만 사용합니다(기존 동작과 동일).
CAS_CC_API_KEY = os.getenv("CAS_CC_API_KEY", "")
CAS_CC_BASE    = "https://commonchemistry.cas.org/api"

# ── 한국환경공단 화학물질 정보 조회 서비스(공공데이터포털) ─────────────
# https://www.data.go.kr/data/15149420/openapi.do
# CAS·물질명으로 물질ID·CAS·KE번호·국문/영문명·분자식·분자량·물질분류를 제공.
# 국내 공식 출처라 내장 고시파일 대조 결과를 교차검증하는 데 쓴다.
#   KECO_API_KEY : 공공데이터포털 인증키(디코딩/인코딩 어느 쪽이든 허용)
#   KECO_NCIS_URL: 엔드포인트. 포털에서 운영주소가 바뀌면 코드 수정 없이 교체 가능.
#   KECO_NCIS_PARAM: CAS 검색 파라미터명. 비워두면 후보들을 순서대로 자동 탐색한다.
KECO_API_KEY    = os.getenv("KECO_API_KEY", "")
KECO_NCIS_URL   = os.getenv("KECO_NCIS_URL", "https://apis.data.go.kr/B552584/kecoapi/ncissbstn")
KECO_NCIS_PARAM = os.getenv("KECO_NCIS_PARAM", "")

# ── 접속 잠금(비밀번호) ─────────────────────────────────────────────
# 아이디/비밀번호를 환경변수로 설정하면 전체 사이트에 로그인창이 걸립니다.
# 둘 중 하나라도 비어 있으면 잠금은 꺼진 상태(누구나 접속)로 동작합니다.
AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASS = os.getenv("AUTH_PASS", "")
AUTH_ON   = bool(AUTH_USER and AUTH_PASS)

APP_DIR   = pathlib.Path(__file__).parent
STATIC    = APP_DIR / "static"
CACHE_DIR = pathlib.Path(os.getenv("CACHE_DIR", "/tmp/cache"))  # 클라우드에서 항상 쓰기 가능
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 60 * 60 * 24 * 30                 # 30일

PC  = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PCV = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
UA  = {"User-Agent": "chem-relay/1.0 (self-hosted; Synology)"}

CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")
H_RE   = re.compile(r"H\d{3}[A-Za-z]?(?:\+H\d{3})*")

app = FastAPI(title="Chem Hazard Relay")

# 콘솔을 다른 주소(예: Web Station)에서 열어도 호출되도록 CORS 허용.
# 같은 컨테이너에서 UI를 함께 서비스하면 사실 필요 없지만, 유연성을 위해 열어둠.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
)

# ── 접속 잠금 미들웨어 ──────────────────────────────────────────────
# AUTH_ON 이면 모든 요청에 대해 브라우저 기본 로그인창(HTTP Basic)을 요구.
# /api/health 는 배포 상태 점검용이라 잠금에서 제외.
class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not AUTH_ON or request.url.path == "/api/health":
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            import base64
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pw = raw.partition(":")
                ok = (secrets.compare_digest(user, AUTH_USER) and
                      secrets.compare_digest(pw, AUTH_PASS))
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="chem-relay"'})
        return await call_next(request)

app.add_middleware(BasicAuthMiddleware)

# ----------------------------------------------------------------------
# 캐시
# ----------------------------------------------------------------------
def _cache_path(key: str) -> pathlib.Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"

def cache_get(key: str):
    p = _cache_path(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < CACHE_TTL:
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return None
    return None

def cache_set(key: str, value: dict):
    try:
        _cache_path(key).write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    except Exception:
        pass

# ----------------------------------------------------------------------
# 국내 고시 조회 데이터 (기존화학물질 별표1~3 · 유해성심사결과)
#   app/data/*.json 은 업로드된 고시 엑셀/hwpx를 CAS 조회용으로 변환한 것.
#   정부 공개 API가 없으므로, 공식 고시 파일을 서버에 내장해 CAS로 즉시 대조.
# ----------------------------------------------------------------------
DATA_DIR = APP_DIR / "data"
def _load_json(name):
    try:
        return json.loads((DATA_DIR / name).read_text("utf-8"))
    except Exception:
        return {}
EXISTING_CHEM = _load_json("existing_chemicals.json")   # {CAS: {keno, name, table}}
HAZARD_REVIEW = _load_json("hazard_review.json")         # {CAS: {no, 해당여부, 분류표시, 구분}}
# 지정물질 마스터: 화학물질_20260804.xlsx 변환. {CAS: {desig:[{label,note}], detail, name}}
#   유독물질·사고대비·제한·금지·중점·잔류·등록대상 등을 CAS로 대조.
DESIG_MASTER = _load_json("designated_master.json")

# 지정물질 목록: app/data/designated/*.json 을 모두 로드.
#   각 파일 형식: {"label":"제한물질","source":"기후에너지환경부 고시 제....","cas":["50-00-0", ...]}
#   또는 {"label":..., "source":..., "items":{"50-00-0":"비고/조건", ...}}
# 파일을 추가/교체하면 자동 반영(코드 수정 불필요).
DESIGNATED = []   # [{label, source, cas:set}]
def _load_designated():
    out = []
    ddir = DATA_DIR / "designated"
    if ddir.is_dir():
        for fp in sorted(ddir.glob("*.json")):
            try:
                d = json.loads(fp.read_text("utf-8"))
                cas = set(d.get("cas") or list((d.get("items") or {}).keys()))
                out.append({"label": d.get("label", fp.stem),
                            "source": d.get("source", ""),
                            "cas": cas,
                            "items": d.get("items") or {}})
            except Exception:
                pass
    return out
DESIGNATED = _load_designated()

# 각 물질 결과물 엑셀의 '(참고) 분류표시' 시트(국외 DB별 유해성 분류·표시)를
# scripts/build_classification_reference.py 로 미리 추출한 결과.
# {CAS: {"name":..., "rows":[{"category","item","grade","db","source_url"}, ...]}}
CLASSIFICATION_REF = _load_json("classification_reference.json")

# US NTP 발암성 보고서(Report on Carcinogens, 15판) CASRN 목록.
# 의회 보고서라 신규 발간 시에만 바뀌므로 scripts/build_ntp_roc.py 로 미리 추출해 둔다.
# {CAS: {"name","listing":"Known|RAHC","listing_ko"}}
NTP_ROC = _load_json("ntp_roc.json")

# ----------------------------------------------------------------------
# 이성질체·무수물·수화물·중수소치환체 매칭 (CAS 기준 기존화학물질 판단 보완용)
#   입력 CAS로 유추한 물질명에서 cis/trans/(E)/(Z)류 이성질체 표기, 수화물·무수물
#   표기(dihydrate, pentahydrate 등 숫자 접두어 포함), 중수소 치환 표기(-D4, -D6,
#   OD 등)를 제거한 뒤, 기존화학물질 목록의 물질명과 대조한다. 공백·구두점 차이는
#   완전히 무시(공백 없는 압축 비교)하고, 등록명이 "A ; B" 형태로 이명을 함께
#   담고 있으면 각 이명을 따로 대조한다. 매칭되면(다른 CAS인데 같은 base name)
#   고시 제3조(이성질체·수화물·무수물 등) 해당 가능성이 있으므로 화학물질정보
#   처리시스템에서 반드시 확인하도록 안내한다.
# ----------------------------------------------------------------------
_CASNO_ANNOTATION_RE = re.compile(r"(?i)\(\s*cas\s*no\.?\s*[\d\-]+\s*\)")
# cis/trans, (E)/(Z)/(R)/(S), 자리번호가 붙은 입체표기((1E), (3R,4S), (6E) 등)
_ISOMER_PREFIX_RE = re.compile(r"(?i)\b(cis|trans)\b[-\s]*|\(\s*[\d,\s]*[EZRS]\s*\)[-\s]*")
# mono/di/tri...hydrate, anhydrous
_HYDRATE_RE = re.compile(
    r"(?i)\b(mono|di|tri|tetra|penta|hexa|hepta|octa|nona|deca)?\s*hydrate\b|\banhydrous\b")
# 중수소 치환 표기: 물질명 뒤에 붙는 -D4, -D6, -D12, -D(숫자 없이) 또는 말미의 OD
_DEUTERIUM_RE = re.compile(r"(?i)[-\s]d\d{0,2}\b(?!\w)|\bod\b\s*$")
# 이름 맨 앞의 자리번호 접두어(예: "1,2-", "1,1,2,2-") — 등록명·조회명 중
# 한쪽에만 자리번호가 붙어 있어도 대조되도록 보조 키로 사용
_LEADING_LOCANT_RE = re.compile(r"^\d+(,\d+)*-")

def _normalize_isomer_name(name: str, strip_locant: bool = False) -> str:
    if not name:
        return ""
    s = name.lower()
    s = _CASNO_ANNOTATION_RE.sub(" ", s)
    s = _ISOMER_PREFIX_RE.sub(" ", s)
    s = _HYDRATE_RE.sub(" ", s)
    s = _DEUTERIUM_RE.sub(" ", s)
    s = s.strip()
    if strip_locant:
        s = _LEADING_LOCANT_RE.sub("", s)
    # 공백·하이픈 등 구두점 차이(예: "toluenesulfonic" vs "toluene sulfonic")를
    # 흡수하기 위해 공백을 남기지 않고 완전히 압축한 키를 사용한다.
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s

def _name_keys(name: str) -> set:
    return {k for k in (_normalize_isomer_name(name, False), _normalize_isomer_name(name, True)) if k}

def _build_name_index():
    idx = {}
    for cas, rec in EXISTING_CHEM.items():
        full_name = rec.get("name") or ""
        # 등록명이 "A ; B" 형태로 여러 이명을 함께 담고 있으면 각각 따로 대조한다.
        for synonym in (full_name.split(";") if full_name else []):
            for key in _name_keys(synonym.strip()):
                idx.setdefault(key, []).append({
                    "cas": cas, "name": full_name,
                    "keno": rec.get("keno"), "table": rec.get("table"),
                })
    return idx
EXISTING_NAME_INDEX = _build_name_index()

def isomer_hydrate_check(names, exclude_cas: str = None) -> dict:
    """물질명뿐 아니라 이명(동의어)까지 함께 대조한다.
    PubChem 대표명이 등록명과 표기가 크게 달라도(예: 'Silicic acid (H2SiO3), disodium
    salt, pentahydrate' vs 등록명 'Disodium metasilicate'), 동의어 목록 중 하나가
    등록명과 더 가깝게 일치하는 경우가 많다(예: 'Disodium metasilicate pentahydrate')."""
    if isinstance(names, str):
        names = [names]
    keys = set()
    for n in names or []:
        keys |= _name_keys(n)
    if not keys:
        return {"matched": False, "candidates": []}
    seen, hits = set(), []
    for key in keys:
        for h in EXISTING_NAME_INDEX.get(key, []):
            if h["cas"] == exclude_cas or h["cas"] in seen:
                continue
            seen.add(h["cas"])
            hits.append(h)
    return {"matched": bool(hits), "candidates": hits}

def designated_hits(cas: str):
    """CAS가 포함된 모든 지정물질 목록을 반환."""
    hits = []
    for g in DESIGNATED:
        if cas and cas in g["cas"]:
            hits.append({"label": g["label"], "source": g["source"],
                         "note": g["items"].get(cas, "")})
    return hits

def korea_notice_lookup(cas: str) -> dict:
    """CAS로 기존화학물질·유해성심사·지정물질 현황을 고시 데이터에서 대조."""
    ex = EXISTING_CHEM.get(cas or "")
    rv = HAZARD_REVIEW.get(cas or "")
    hits = designated_hits(cas or "")
    # 지정물질 마스터(화학물질_20260804) 대조 결과 병합
    dm = DESIG_MASTER.get(cas or "")
    master_detail = ""
    if dm:
        master_detail = dm.get("detail", "")
        for x in dm.get("desig", []):
            hits.append({"label": x.get("label"), "source": "화학물질 지정 마스터(기후에너지환경부 자료)",
                         "note": x.get("note", "")})
    # 유해성심사에서 '인체등유해성물질 해당'이면 지정물질 목록에도 추가 표기
    if rv and "해당함" in str(rv.get("해당여부", "")):
        hits = [{"label": "인체등유해성물질", "source": "화학물질의 유해성심사결과 고시",
                 "note": rv.get("해당여부", "")}] + hits
    if ex:
        hits = [{"label": "기존화학물질", "source": f"기존화학물질 고시 {ex.get('table','')}",
                 "note": f"고유번호 {ex.get('keno','')}"}] + hits
    # 라벨 중복 제거(첫 출처 유지)
    seen, uniq = set(), []
    for h in hits:
        if h["label"] in seen: continue
        seen.add(h["label"]); uniq.append(h)
    return {
        "existing": ({
            "is_existing": True,
            "keno": ex.get("keno"),
            "table": ex.get("table"),
            "name": ex.get("name"),
        } if ex else {"is_existing": False}),
        "review": ({
            "reviewed": True,
            "no": rv.get("no"),
            "result": rv.get("해당여부"),
            "classification": (rv.get("분류표시") or "")[:600],
            "category": rv.get("구분"),
        } if rv else {"reviewed": False}),
        "designated": uniq,          # 지정물질 현황(전체)
        "designated_detail": master_detail,   # 유해특성·함량기준 상세
        "counts": {"existing_db": len(EXISTING_CHEM), "review_db": len(HAZARD_REVIEW),
                   "designated_master": len(DESIG_MASTER), "designated_lists": len(DESIGNATED)},
    }

# ----------------------------------------------------------------------
# UVCB 등 PubChem 미수록 물질 폴백 (엑셀 예시 물질)
# ----------------------------------------------------------------------
FALLBACK = {
    "97926-71-1": {
        "found": True, "source": "규제자료(수기 정리)",
        "identity": {
            "title": "Hydrocarbons, C27-42, naphthenic",
            "title_ko": "탄화수소류, C27-42, 나프텐계 (나프텐계 베이스오일)",
            "cas": "97926-71-1", "ec": "308-290-3", "ke": "KE-20077",
            "formula": "UVCB — 단일 분자식 없음", "mw": None, "iupac": None,
            "cid": None, "structure_url": None,
        },
        "note": "이 물질은 UVCB(석유유분)라 구조 기반 DB인 PubChem에 개별 레코드가 없습니다. "
                "아래 GHS는 EU 규제자료에서 확인된 값입니다.",
        "ghs": {
            "signal": "Danger", "pictos": ["발암성"],
            "statements": [{"h": "H350",
                            "t": "암을 일으킬 수 있음 (Note L: DMSO 추출물 3% 미만 시 미분류)",
                            "src": "EU CLP Annex VI, Index 649-524-00-7"}],
            "provenance": "EU CLP Annex VI 조화분류", "source_url": None,
        },
        "korea": {"status": "미분류(확인대상)",
                  "detail": "KE-20077 기존화학물질 · '유해성정보 미분류 확인 대상'"},
    }
}

# ----------------------------------------------------------------------
# PubChem 조회
# ----------------------------------------------------------------------
async def _get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()

def _find_section(node, heading):
    if not node:
        return None
    if node.get("TOCHeading") == heading:
        return node
    for s in (node.get("Section") or node.get("Record", {}).get("Section") or []):
        f = _find_section(s, heading)
        if f:
            return f
    return None

def _parse_ghs(pugview: dict) -> dict:
    sec = _find_section(pugview, "GHS Classification")
    info = (sec or {}).get("Information", []) or []
    signal, pictos, statements, provenance = "", [], [], ""
    for it in info:
        name = it.get("Name", "") or ""
        vals = [x.get("String", "") for x in (it.get("Value", {}).get("StringWithMarkup") or [])]
        if name == "Signal":
            signal = vals[0] if vals else signal
        elif name.startswith("Pictogram"):
            for sw in (it.get("Value", {}).get("StringWithMarkup") or []):
                for m in (sw.get("Markup") or []):
                    if m.get("Extra"):
                        pictos.append(m["Extra"])
                for t in re.split(r"[,;]", sw.get("String", "")):
                    t = t.strip()
                    if t:
                        pictos.append(t)
        elif "Hazard Statements" in name:
            for s in vals:
                m = H_RE.search(s)
                if m:
                    t = s.replace(m.group(0), "")
                    t = re.sub(r"^[\s:()%\d]+", "", t).strip()
                    statements.append({"h": m.group(0), "t": t,
                                       "src": "PubChem 집계 (ECHA C&L Inventory 등)"})
        elif "Aggregated" in name or re.search(r"provided by|notifications", " ".join(vals), re.I):
            for s in vals:
                if re.search(r"provided by|notifications|ECHA", s, re.I):
                    provenance = s
                    break
    if not provenance:
        m = re.search(r"Aggregated GHS[^\"]+", json.dumps(info))
        if m:
            provenance = m.group(0)
    # 중복 제거
    seen, picto_clean = set(), []
    for p in pictos:
        if p and len(p) < 40 and p not in seen:
            seen.add(p); picto_clean.append(p)
    return {"signal": signal, "pictos": picto_clean,
            "statements": statements, "provenance": provenance}

async def fetch_pubchem(q: str) -> dict:
    from urllib.parse import quote
    async with httpx.AsyncClient() as client:
        # 1) name/CAS -> CID
        try:
            d = await _get_json(client, f"{PC}/compound/name/{quote(q)}/cids/JSON")
        except Exception:
            return {"found": False}
        cids = (d or {}).get("IdentifierList", {}).get("CID", [])
        if not cids:
            return {"found": False}
        cid = cids[0]

        # 2) 속성 + 동의어 병렬
        prop_url = (f"{PC}/compound/cid/{cid}/property/"
                    "MolecularFormula,MolecularWeight,IUPACName,Title,"
                    "CanonicalSMILES,IsomericSMILES,InChIKey/JSON")
        syn_url  = f"{PC}/compound/cid/{cid}/synonyms/JSON"
        prop, syn = await asyncio.gather(
            _get_json(client, prop_url), _get_json(client, syn_url),
            return_exceptions=True)
        p = {}
        if isinstance(prop, dict):
            props = prop.get("PropertyTable", {}).get("Properties", [])
            p = props[0] if props else {}
        syns = []
        if isinstance(syn, dict):
            info = syn.get("InformationList", {}).get("Information", [])
            syns = info[0].get("Synonym", []) if info else []
        cas = next((s for s in syns if CAS_RE.match(s)), q if CAS_RE.match(q) else None)
        # EC(EINECS) 번호: 3-3-1 자릿수 패턴
        EC_RE = re.compile(r"^\d{3}-\d{3}-\d$")
        ec = next((re.sub(r"^EINECS\s*", "", s) for s in syns
                   if EC_RE.match(re.sub(r"^EINECS\s*", "", s.strip()))), None)
        # 다른 이름(Other names): CAS·EC·순수숫자코드·타이틀 제외한 대표 동의어 몇 개
        title = p.get("Title") or q
        other = []
        for s in syns:
            s2 = s.strip()
            if not s2 or s2.lower() == title.lower(): continue
            if CAS_RE.match(s2) or EC_RE.match(s2): continue
            if re.match(r"^[\d\-\s]+$", s2): continue          # 숫자코드
            if re.match(r"^(EINECS|UNII|DTXSID|CHEBI|CHEMBL|MFCD|DSSTox|NSC)\b", s2, re.I): continue
            if len(s2) > 60: continue
            other.append(s2)
            if len(other) >= 8: break

        # 3) GHS
        ghs = {"signal": "", "pictos": [], "statements": [], "provenance": ""}
        try:
            gv = await _get_json(client, f"{PCV}/data/compound/{cid}/JSON?heading=GHS+Classification")
            ghs = _parse_ghs(gv)
        except Exception:
            pass

        return {
            "found": True, "source": "PubChem (실시간)",
            "identity": {
                "title": p.get("Title") or q, "title_ko": None,
                "cas": cas, "ec": ec, "ke": None,
                "formula": p.get("MolecularFormula"),
                "mw": p.get("MolecularWeight"),
                "iupac": p.get("IUPACName"),
                "smiles": p.get("IsomericSMILES") or p.get("CanonicalSMILES"),
                "inchikey": p.get("InChIKey"),
                "other_names": other,
                "cid": cid,
                "structure_url": f"{PC}/compound/cid/{cid}/PNG?image_size=300x300",
            },
            "ghs": {**ghs,
                    "source_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=GHS-Classification"},
            "record_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        }

# ----------------------------------------------------------------------
# CAS Common Chemistry 조회 (참고자료 1순위)
#   공식 API는 X-API-KEY 헤더가 필요합니다(무료/유료 등록:
#   https://www.cas.org/services/commonchemistry-api). 키가 없으면
#   자동으로 건너뛰고 PubChem(2순위)만 사용합니다.
# ----------------------------------------------------------------------
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
def _strip_tags(s):
    return _TAG_RE.sub("", s or "").strip()

async def fetch_cas_cc(q: str) -> dict:
    if not CAS_CC_API_KEY:
        return {"found": False, "reason": "no_api_key"}
    headers = {**UA, "X-API-KEY": CAS_CC_API_KEY}
    try:
        async with httpx.AsyncClient() as client:
            if CAS_RE.match(q):
                r = await client.get(f"{CAS_CC_BASE}/detail", params={"cas_rn": q},
                                      headers=headers, timeout=15)
                if r.status_code != 200:
                    return {"found": False, "reason": f"http_{r.status_code}"}
                d = r.json()
            else:
                r = await client.get(f"{CAS_CC_BASE}/search", params={"q": q},
                                      headers=headers, timeout=15)
                if r.status_code != 200:
                    return {"found": False, "reason": f"http_{r.status_code}"}
                results = (r.json() or {}).get("results") or []
                rn = results[0].get("rn") if results else None
                if not rn:
                    return {"found": False, "reason": "no_match"}
                r2 = await client.get(f"{CAS_CC_BASE}/detail", params={"cas_rn": rn},
                                       headers=headers, timeout=15)
                if r2.status_code != 200:
                    return {"found": False, "reason": f"http_{r2.status_code}"}
                d = r2.json()
    except Exception as e:
        return {"found": False, "reason": f"error:{e}"}

    other = [_strip_tags(s) for s in (d.get("synonyms") or [])]
    other = [s for s in other if s][:8]
    rn = d.get("rn")
    return {
        "found": True,
        "cas": rn,
        "name": _strip_tags(d.get("name")),
        "other_names": other,
        "formula": _strip_tags(d.get("molecularFormula")) or None,
        "mw": d.get("molecularMass"),
        "smiles": d.get("canonicalSmile"),
        "inchikey": d.get("inchiKey"),
        "record_url": f"https://commonchemistry.cas.org/results/{rn}" if rn else None,
    }

def _dedup_names(names):
    seen, out = set(), []
    for n in names:
        n2 = (n or "").strip()
        if not n2 or n2.lower() in seen:
            continue
        seen.add(n2.lower())
        out.append(n2)
    return out

def merge_identity(casc: dict, pub: dict) -> dict:
    """참고자료 우선순위 병합: 1) CAS Common Chemistry  2) PubChem.
    CAS CC에 없는 필드(IUPAC명·분자량·구조식·CID)는 PubChem으로 보완한다."""
    pid = (pub or {}).get("identity", {}) or {}
    if casc and casc.get("found"):
        merged_other = _dedup_names(list(casc.get("other_names") or []) + list(pid.get("other_names") or []))
        return {
            "title": casc.get("name") or pid.get("title"),
            "title_ko": pid.get("title_ko"),
            "cas": casc.get("cas") or pid.get("cas"),
            "ec": pid.get("ec"),
            "formula": casc.get("formula") or pid.get("formula"),
            "mw": casc.get("mw") or pid.get("mw"),
            "iupac": pid.get("iupac"),
            "smiles": casc.get("smiles") or pid.get("smiles"),
            "inchikey": casc.get("inchikey") or pid.get("inchikey"),
            "other_names": merged_other[:8],
            "cid": pid.get("cid"),
            "structure_url": pid.get("structure_url"),
            "source_no": 1,
            "source_label": "1) CAS Common Chemistry",
            "cas_cc_url": casc.get("record_url"),
        }
    ident = dict(pid)
    ident["source_no"] = 2
    ident["source_label"] = "2) PubChem" if CAS_CC_API_KEY else "2) PubChem (CAS Common Chemistry API 키 미설정)"
    ident["cas_cc_url"] = None
    return ident

# ----------------------------------------------------------------------
# 물리화학적 특성 (옥탄올-물 분배계수 · 끓는점 · 증기압)
#   1순위: PubChem 실험값(PUG-View) — HSDB·ICSC 등 원출처가 함께 표시된다.
#   2순위: EPA EPI Suite Web 공개 API — PubChem에 없는 항목만 보완한다.
#          EPI Suite는 실측(EXPERIMENTAL)과 모델 추정(ESTIMATED)을 구분해 주므로
#          화면에도 그대로 구분해 표시한다.
# ----------------------------------------------------------------------
PHYS_HEADINGS = {"logkow": "LogP", "boiling_point": "Boiling Point", "vapor_pressure": "Vapor Pressure"}
EPISUITE_API = "https://episuite.dev/api/submit"
EPISUITE_SITE = "https://episuite.dev/"

async def _pubchem_prop(client, cid, heading):
    """PUG-View에서 해당 항목의 (값, 원출처) 목록을 뽑는다."""
    try:
        r = await client.get(f"{PCV}/data/compound/{cid}/JSON", params={"heading": heading})
        if r.status_code != 200:
            return []
        d = r.json()
    except Exception:
        return []
    refs = {ref.get("ReferenceNumber"): ref.get("SourceName")
            for ref in (d.get("Record", {}).get("Reference") or [])}
    out = []
    def walk(node):
        for it in (node.get("Information") or []):
            v = it.get("Value", {}) or {}
            vals = [sw.get("String") for sw in (v.get("StringWithMarkup") or []) if sw.get("String")]
            if not vals and v.get("Number"):
                num = " ".join(str(x) for x in v["Number"])
                vals = [f"{num} {v.get('Unit','')}".strip()]
            if vals:
                out.append({"value": vals[0].strip(),
                            "source": refs.get(it.get("ReferenceNumber")) or "PubChem"})
        for s in (node.get("Section") or []):
            walk(s)
    walk(d.get("Record", {}) or {})
    return out

def _epi_pick(block):
    """EPI Suite 항목에서 대표값(실측 우선) + 추정값을 뽑는다."""
    if not isinstance(block, dict):
        return None
    sel = block.get("selectedValue") or {}
    est = block.get("estimatedValue") or {}
    val = sel.get("value")
    if val is None and isinstance(est, dict):
        val = est.get("value")
        sel = {"valueType": "ESTIMATED", "units": est.get("units"), "source": est.get("source")}
    if val is None:
        return None
    unit = (sel.get("units") or "").strip()
    unit = "°C" if unit == "celsius" else unit
    try:
        shown = f"{round(float(val), 4):g}"
    except Exception:
        shown = str(val)
    kind = "실측" if str(sel.get("valueType", "")).upper() in ("EXPERIMENTAL", "EVIDENCE") else "추정"
    est_val = est.get("value") if isinstance(est, dict) else None
    try:
        est_shown = f"{round(float(est_val), 4):g}" if est_val is not None else None
    except Exception:
        est_shown = str(est_val) if est_val is not None else None
    return {"value": f"{shown} {unit}".strip(), "kind": kind,
            "estimated": f"{est_shown} {unit}".strip() if est_shown else None,
            "source_raw": str(sel.get("source") or "")}

async def fetch_episuite(cas: str) -> dict:
    empty = {"found": False, "record_url": EPISUITE_SITE}
    if not cas or not CAS_RE.match(cas):
        return empty
    try:
        async with httpx.AsyncClient(timeout=40, headers={**UA, "Accept": "application/json"}) as client:
            r = await client.get(EPISUITE_API,
                                 params={"cas": cas, "modules": "logKow,physicalProperties"})
            if r.status_code != 200:
                return {**empty, "reason": f"http_{r.status_code}"}
            d = r.json()
    except Exception as e:
        return {**empty, "reason": f"error:{e}"}
    props = {}
    for key, api_key in (("logkow", "logKow"), ("boiling_point", "boilingPoint"),
                         ("vapor_pressure", "vaporPressure")):
        picked = _epi_pick(d.get(api_key))
        if picked:
            props[key] = picked
    cp = d.get("chemicalProperties") or {}
    return {"found": bool(props), "props": props, "name": cp.get("name"),
            "record_url": EPISUITE_SITE}

async def fetch_phys_props(cid, cas: str) -> dict:
    """PubChem 실험값을 먼저 채우고, 빈 항목만 EPI Suite로 보완한다."""
    result = {k: [] for k in PHYS_HEADINGS}
    if cid:
        try:
            async with httpx.AsyncClient(timeout=25, headers=UA) as client:
                got = await asyncio.gather(
                    *[_pubchem_prop(client, cid, h) for h in PHYS_HEADINGS.values()],
                    return_exceptions=True)
            for key, res in zip(PHYS_HEADINGS.keys(), got):
                result[key] = res[:3] if isinstance(res, list) else []
        except Exception:
            pass
    missing = [k for k, v in result.items() if not v]
    epi = {"found": False, "record_url": EPISUITE_SITE}
    if missing and cas:
        epi = await fetch_episuite(cas)
    return {"pubchem": result, "episuite": epi, "missing": missing,
            "episuite_url": EPISUITE_SITE}

# ----------------------------------------------------------------------
# 한국환경공단 화학물질 정보 조회 서비스 (공공데이터포털 오픈API)
#   국내 공식 출처로 KE번호·국문명·분자식·물질분류를 받아, 내장 고시파일로 한
#   기존/신규 판정을 교차검증한다. 키가 없거나 실패하면 조용히 건너뛴다.
#   공식 규격 문서를 코드에 하드코딩하지 않고, 파라미터명·응답 구조를 유연하게
#   탐색하도록 만들어 포털 쪽 규격이 조금 달라도 동작하게 한다.
# ----------------------------------------------------------------------
_KECO_CAS_PARAMS  = ["casNo", "casno", "srchCasNo", "searchCasNo", "casRn", "searchWord", "srchWord"]
_KECO_NAME_PARAMS = ["sbstnNm", "chemNm", "searchWord", "srchWord", "korNm"]
_keco_param_cache = {}   # {"cas": "casNo", "name": "sbstnNm"} — 한 번 통한 이름을 재사용

def _keco_service_key() -> str:
    """포털 인증키는 인코딩/디코딩 두 형태로 배포된다. httpx가 다시 인코딩하므로
    항상 '디코딩된' 형태로 만들어 넘긴다(그래야 %2F 등이 이중 인코딩되지 않음)."""
    from urllib.parse import unquote
    k = (KECO_API_KEY or "").strip()
    return unquote(k) if "%" in k else k

def _keco_find_items(payload):
    """data.go.kr 응답(JSON/XML 파싱본)에서 레코드 목록을 재귀적으로 찾아낸다."""
    found = []
    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k.lower() in ("item", "items", "row") and v is not None:
                    if isinstance(v, list):
                        found.extend([x for x in v if isinstance(x, dict)])
                    elif isinstance(v, dict):
                        # {"items": {"item": [...]}} 형태
                        inner = v.get("item") if "item" in v else None
                        if isinstance(inner, list):
                            found.extend([x for x in inner if isinstance(x, dict)])
                        elif isinstance(inner, dict):
                            found.append(inner)
                        else:
                            found.append(v)
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk(payload)
    return found

def _keco_parse(text: str):
    """JSON이면 그대로, XML이면 dict로 바꿔 레코드 목록과 에러메시지를 돌려준다."""
    t = (text or "").strip()
    if not t:
        return [], "empty"
    if t.startswith("{") or t.startswith("["):
        try:
            data = json.loads(t)
        except Exception:
            return [], "json_parse_error"
    else:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(t, "xml") if "<?xml" in t[:80] else BeautifulSoup(t, "html.parser")
        except Exception:
            return [], "xml_parse_error"
        rows = soup.find_all(re.compile(r"^item$", re.I))
        recs = []
        for row in rows:
            rec = {}
            for ch in row.find_all(recursive=False):
                rec[ch.name] = ch.get_text(strip=True)
            if rec:
                recs.append(rec)
        if recs:
            return recs, None
        err = soup.find(re.compile(r"^(errMsg|returnAuthMsg|resultMsg)$", re.I))
        return [], (err.get_text(strip=True) if err else "no_items")
    items = _keco_find_items(data)
    if items:
        return items, None
    # 에러 메시지 추출
    msg = ""
    def findmsg(n):
        nonlocal msg
        if isinstance(n, dict):
            for k, v in n.items():
                if k.lower() in ("errmsg", "returnauthmsg", "resultmsg") and isinstance(v, str) and not msg:
                    msg = v
                findmsg(v)
        elif isinstance(n, list):
            for x in n:
                findmsg(x)
    findmsg(data)
    return [], (msg or "no_items")

def _keco_pick(rec: dict, *needles):
    """필드명이 규격마다 조금씩 달라, 키 이름에 특정 문자열이 들어간 값을 찾아온다."""
    for k, v in (rec or {}).items():
        kl = str(k).lower()
        if any(n in kl for n in needles):
            s = str(v).strip()
            if s and s.lower() not in ("none", "null"):
                return s
    return ""

async def fetch_keco_ncis(cas: str = None, name: str = None) -> dict:
    empty = {"enabled": bool(KECO_API_KEY), "found": False,
             "portal_url": "https://www.data.go.kr/data/15149420/openapi.do"}
    if not KECO_API_KEY:
        return {**empty, "reason": "no_api_key"}
    query = (cas or "").strip() or (name or "").strip()
    if not query:
        return {**empty, "reason": "no_query"}

    kind = "cas" if (cas or "").strip() else "name"
    candidates = ([KECO_NCIS_PARAM] if KECO_NCIS_PARAM else []) \
        + ([_keco_param_cache[kind]] if _keco_param_cache.get(kind) else []) \
        + (_KECO_CAS_PARAMS if kind == "cas" else _KECO_NAME_PARAMS)
    seen, ordered = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c); ordered.append(c)

    last_err = None
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            for pname in ordered:
                params = {"serviceKey": _keco_service_key(), "pageNo": 1, "numOfRows": 5,
                          "returnType": "json", "type": "json", pname: query}
                try:
                    r = await client.get(KECO_NCIS_URL, params=params)
                except Exception as e:
                    last_err = f"error:{e}"
                    continue
                items, err = _keco_parse(r.text)
                if items:
                    _keco_param_cache[kind] = pname
                    rec = items[0]
                    return {
                        **empty, "found": True, "param": pname, "count": len(items),
                        "cas": _keco_pick(rec, "cas"),
                        "keno": _keco_pick(rec, "keno", "ke_no", "kenum", "고유"),
                        "name_ko": _keco_pick(rec, "korn", "knm", "kornm", "국문", "namekor"),
                        "name_en": _keco_pick(rec, "engn", "enm", "engnm", "영문", "nameeng"),
                        "formula": _keco_pick(rec, "formula", "분자식", "mlcl"),
                        "mw": _keco_pick(rec, "weight", "분자량", "mlclwt"),
                        "category": _keco_pick(rec, "clsf", "category", "분류", "type"),
                        "raw": rec,
                    }
                last_err = err
                # 인증키 문제면 파라미터를 더 시도해도 소용없다.
                if err and ("KEY" in str(err).upper() or "인증" in str(err)):
                    break
    except Exception as e:
        return {**empty, "reason": f"error:{e}"}
    return {**empty, "reason": last_err or "not_found"}

# ----------------------------------------------------------------------
# 화학물질정보처리시스템(KREACH) 정보공개 화면 조회
#   공개 API가 없어 검색→상세 2단계 폼 POST를 그대로 재현한다.
#   사이트 개편 시 이 함수만 고치면 되도록 분리해 둔다.
# ----------------------------------------------------------------------
KREACH_LIST_URL   = "https://kreach.mcee.go.kr/repwrt/mttrInfoOpen/mttrInfoOpenList.do"
KREACH_DETAIL_URL = "https://kreach.mcee.go.kr/repwrt/mttrInfoOpen/mttrInfoOpenDetail.do"
KREACH_LINKS = {
    "기후에너지환경부 화학물질정보처리시스템": "https://kreach.mcee.go.kr/repwrt/index.do",
    "고용노동부 MSDS": "https://msds.kosha.or.kr/MSDSInfo/",
    "소방청 국가위험물통합정보": "https://hazmat.nfa.go.kr/",
}
_FN_DETAIL_RE = re.compile(r"fn_detail\('([^']+)'(?:\s*,\s*'([^']*)')?\)")

def _expand_rowspan_rows(tbody):
    """rowspan(세로 병합) 셀을 각 행에 값을 복제해 모든 행이 전체 컬럼을 갖도록 펼친다.
    KREACH 유해성 분류 표는 그림문자·신호어·검토기준일(또는 기업 수) 열이 rowspan으로
    첫 행에만 있고 나머지 행은 해당 <td> 자체가 생략되어 있어, 그대로 읽으면 첫 행만
    유효한 것처럼(컬럼 수가 많은 것처럼) 보이고 나머지 행이 누락된다."""
    pending = {}  # col_idx -> [remaining_rows, text]
    grid = []
    for tr in tbody.find_all("tr"):
        row, col, ti = {}, 0, 0
        tds = tr.find_all("td")
        while ti < len(tds) or any(v[0] > 0 for v in pending.values()):
            if col in pending and pending[col][0] > 0:
                row[col] = pending[col][1]
                pending[col][0] -= 1
                if pending[col][0] == 0:
                    del pending[col]
                col += 1
                continue
            if ti >= len(tds):
                break
            td = tds[ti]; ti += 1
            text = td.get_text(" ", strip=True)
            rowspan = int(td.get("rowspan") or 1)
            colspan = int(td.get("colspan") or 1)
            for k in range(colspan):
                row[col + k] = text
                if rowspan > 1:
                    pending[col + k] = [rowspan - 1, text]
            col += colspan
        maxcol = (max(row.keys()) + 1) if row else 0
        grid.append([row.get(i, "") for i in range(maxcol)])
    return grid

def _kreach_parse_tab(soup, container_id, tab_label):
    box = soup.find(id=container_id)
    if not box:
        return [], []
    haz_rows, use_rows = [], []
    for table in box.find_all("table"):
        cap = table.find("caption")
        cap_text = cap.get_text(strip=True) if cap else ""
        body = table.find("tbody")
        if not body:
            continue
        if "유해성" in cap_text:
            for row in _expand_rowspan_rows(body):
                if len(row) < 5 or "없습니다" in "".join(row):
                    continue
                haz_rows.append({"tab": tab_label, "item": row[0], "grade": row[1],
                                  "picto": row[2], "signal": row[3], "hcode_text": row[4],
                                  "reason": row[5] if len(row) > 5 else ""})
        elif "용도" in cap_text:
            for row in _expand_rowspan_rows(body):
                if len(row) < 2 or "없습니다" in "".join(row):
                    continue
                use_rows.append({"tab": tab_label, "no": row[0], "use": row[1]})
    return haz_rows, use_rows

async def fetch_kreach(cas: str = None, name: str = None) -> dict:
    if not cas and not name:
        return {"implemented": True, "found": False, "reason": "no_query", "links": KREACH_LINKS}
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"implemented": False, "found": False, "reason": "beautifulsoup4 미설치", "links": KREACH_LINKS}

    search_data = {
        "currentPageNo": "1", "recordCountPerPage": "20",
        "MTTR_ID": "", "MNG_NO": "",
        "searchCasNo": cas or "", "searchChmclsNm": "" if cas else (name or ""),
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.post(KREACH_LIST_URL, data=search_data)
            r.raise_for_status()
            m = _FN_DETAIL_RE.search(r.text)
            if not m:
                return {"implemented": True, "found": False, "reason": "not_listed", "links": KREACH_LINKS}
            mttr_id, mng_no = m.group(1), m.group(2) or ""
            detail_data = {**search_data, "MTTR_ID": mttr_id, "MNG_NO": mng_no}
            r2 = await client.post(KREACH_DETAIL_URL, data=detail_data)
            r2.raise_for_status()
            html = r2.text
    except Exception as e:
        return {"implemented": True, "found": False, "reason": f"error:{e}", "links": KREACH_LINKS}

    soup = BeautifulSoup(html, "html.parser")
    haz1, use1 = _kreach_parse_tab(soup, "tab1-contents", "신고")
    haz2, use2 = _kreach_parse_tab(soup, "tab2-contents", "등록")
    return {
        "implemented": True, "found": True,
        "hazard": haz1 + haz2,
        "uses": use1 + use2,
        "detail_url": KREACH_LIST_URL,
        "links": KREACH_LINKS,
    }

# ----------------------------------------------------------------------
# EU CLP — ECHA CHEM(chem.echa.europa.eu) C&L Inventory 실시간 조회
#   비공개 문서화 API는 없지만, 사이트 자체가 호출하는 공개 REST 엔드포인트를
#   그대로 사용한다(로그인·API 키 불필요). 조화분류(Harmonised, CLH)와
#   산업계 신고분류(Industry/Notified, CLH 아님)를 모두 가져온다.
# ----------------------------------------------------------------------
ECHA_CHEM_BASE = "https://chem.echa.europa.eu"

async def fetch_echa_clp(q: str) -> dict:
    empty = {"found": False, "harmonised": [], "industry": [],
             "record_url": None, "rml_id": None}
    try:
        async with httpx.AsyncClient(timeout=15, headers=UA) as client:
            r = await client.get(f"{ECHA_CHEM_BASE}/api-substance/v1/substance",
                                  params={"pageIndex": 1, "pageSize": 1, "searchText": q})
            if r.status_code != 200:
                return empty
            items = (r.json() or {}).get("items") or []
            if not items:
                return empty
            rml = items[0].get("substanceIndex", {}).get("rmlId")
            if not rml:
                return empty
            record_url = f"{ECHA_CHEM_BASE}/{rml}/overview"

            r_info = await client.get(f"{ECHA_CHEM_BASE}/api-cnl-inventory/prominent/overview/info/{rml}")
            info = r_info.json() if r_info.status_code == 200 else {}
            harmonised, industry = [], []

            # 조화분류(CLH) — info의 classificationId가 harmonised일 때만 이 경로로 확보된다.
            if info.get("type") == "harmonised" and info.get("classificationId"):
                r_h = await client.get(
                    f"{ECHA_CHEM_BASE}/api-cnl-inventory/prominent/overview/classifications/harmonised/{info['classificationId']}")
                if r_h.status_code == 200:
                    for it in (r_h.json() or {}).get("items", []):
                        for hs in it.get("hazardStatements", []):
                            harmonised.append({
                                "item": it.get("hazardClassAndCategoryCode", ""),
                                "h": (hs.get("hazardStatementCode") or "").rstrip("*"),
                                "text": hs.get("hazardStatementText", ""),
                            })

            # 산업계 신고분류(Industry/Notified, CLH 아님) — 최다 신고 그룹 1건을 대표로 사용.
            r_list = await client.get(f"{ECHA_CHEM_BASE}/api-cnl-inventory/industry/{rml}/classifications")
            if r_list.status_code == 200:
                cand = [x for x in (r_list.json() or {}).get("items", []) if x.get("status") == "Active"]
                cand.sort(key=lambda x: x.get("substanceNotificationPercentage", 0), reverse=True)
                if cand:
                    top_id = cand[0]["classificationId"]
                    r_i = await client.get(f"{ECHA_CHEM_BASE}/api-cnl-inventory/industry/classification/{top_id}")
                    if r_i.status_code == 200:
                        for it in (r_i.json() or {}).get("items", []):
                            for hs in it.get("hazardStatements", []):
                                industry.append({
                                    "item": it.get("hazardClassAndCategoryCode", ""),
                                    "h": (hs.get("hazardStatementCode") or "").rstrip("*"),
                                    "text": hs.get("hazardStatementText", ""),
                                })

            return {"found": bool(harmonised or industry), "harmonised": harmonised,
                    "industry": industry, "record_url": record_url, "rml_id": rml}
    except Exception:
        return empty

def ntp_lookup(cas: str) -> dict:
    rec = NTP_ROC.get(cas or "")
    if not rec:
        return {"listed": False}
    return {"listed": True, "name": rec.get("name"), "listing": rec.get("listing"),
            "listing_ko": rec.get("listing_ko")}

# ----------------------------------------------------------------------
# 덴마크 CLP 데이터베이스(clp-vejlliste.mst.dk) — 고전 ASP.NET WebForms 검색.
#   __VIEWSTATE 등 postback 토큰을 그대로 재현해 CAS로 검색한다(로그인 불필요).
#   사이트 개편 시 이 함수만 고치면 되도록 분리해 둔다.
# ----------------------------------------------------------------------
DENMARK_CLP_URL = "https://clp-vejlliste.mst.dk/default.aspx"

async def fetch_denmark_clp(cas: str) -> dict:
    from bs4 import BeautifulSoup
    empty = {"found": False, "items": [], "record_url": DENMARK_CLP_URL}
    if not cas:
        return empty
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r1 = await client.get(DENMARK_CLP_URL)
            if r1.status_code != 200:
                return empty
            soup1 = BeautifulSoup(r1.text, "html.parser")
            def field(name):
                el = soup1.find(id=name)
                return el.get("value", "") if el else ""
            data = {
                "__VIEWSTATE": field("__VIEWSTATE"),
                "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
                "__EVENTVALIDATION": field("__EVENTVALIDATION"),
                "txtCASnummer": cas, "txtDanskNavn": "", "txtEinecsNummer": "",
                "txtEinecsNavn": "", "txtBruttoformel": "",
                "OrAnd": "rbAnd", "btnSearch": "Søg",
            }
            r2 = await client.post(DENMARK_CLP_URL, data=data)
            if r2.status_code != 200:
                return empty
            soup2 = BeautifulSoup(r2.text, "html.parser")
    except Exception:
        return empty

    items = []
    for t in soup2.find_all("table"):
        if "CAS nummer:" not in t.get_text():
            continue
        rec = {}
        for tr in t.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                label = tds[0].get_text(strip=True).rstrip(":")
                value = tds[1].get_text(strip=True)
                if label:
                    rec[label] = value
        if rec.get("Klassificering"):
            items.append({
                "cas": rec.get("CAS nummer", ""),
                "einecs": rec.get("EINECS nummer", ""),
                "name_da": rec.get("Dansk navn", ""),
                "name_einecs": rec.get("EINECS navn", ""),
                "formula": rec.get("Bruttoformel", ""),
                "classification": [c.strip() for c in rec["Klassificering"].split(";") if c.strip()],
            })
    return {"found": bool(items), "items": items, "record_url": DENMARK_CLP_URL}

# ----------------------------------------------------------------------
# IARC Monographs — List of Classifications
#   전용 REST API가 없고, 목록 페이지가 불러오는 JS 번들(webapi.iarc.who.int/loc/loc.app.js)
#   안에 전체 데이터(약 1,128건)가 JS 객체 배열 리터럴로 그대로 박혀 있다.
#   IARC가 새 모노그래프를 발간하면 이 파일도 갱신되므로 매 조회마다 새로 받아 파싱한다.
#   사이트 개편 시 이 함수만 고치면 되도록 분리해 둔다.
# ----------------------------------------------------------------------
IARC_APP_JS = "https://webapi.iarc.who.int/loc/loc.app.js"
IARC_LIST_URL = "https://monographs.iarc.who.int/list-of-classifications/"
_IARC_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_IARC_NAME_RE = re.compile(r'name:"((?:[^"\\]|\\.)*)"')
_IARC_GROUP_RE = re.compile(r'group:"((?:[^"\\]|\\.)*)"')
_IARC_CAS_RE = re.compile(r"cas:\[((?:[^\]])*)\]")

def _iarc_split_top_level_objects(blob: str):
    depth, in_str, esc = 0, False, False
    buf, objs = [], []
    for ch in blob[1:-1]:  # strip outer [ ]
        if esc:
            buf.append(ch); esc = False; continue
        if ch == "\\":
            buf.append(ch); esc = True; continue
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        buf.append(ch)
        if depth == 0 and ch == "," and not in_str:
            s = "".join(buf[:-1]).strip()
            if s:
                objs.append(s)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        objs.append(tail)
    return objs

async def fetch_iarc_group(cas: str) -> dict:
    empty = {"listed": False, "record_url": IARC_LIST_URL}
    if not cas:
        return empty
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.get(IARC_APP_JS)
            if r.status_code != 200:
                return empty
            txt = r.text
        start = txt.find("[{name:")
        if start == -1:
            return empty
        depth, in_str, esc, end = 0, False, False, None
        for idx in range(start, len(txt)):
            ch = txt[idx]
            if esc:
                esc = False; continue
            if ch == "\\":
                esc = True; continue
            if ch == '"':
                in_str = not in_str; continue
            if in_str:
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0 and ch == "]":
                    end = idx + 1
                    break
        if not end:
            return empty
        for obj in _iarc_split_top_level_objects(txt[start:end]):
            m_cas = _IARC_CAS_RE.search(obj)
            if not m_cas or f'"{cas}"' not in m_cas.group(0):
                continue
            m_name = _IARC_NAME_RE.search(obj)
            m_group = _IARC_GROUP_RE.search(obj)
            return {
                "listed": True,
                "name": _IARC_TAG_RE.sub("", m_name.group(1)) if m_name else "",
                "group": m_group.group(1) if m_group else "",
                "record_url": IARC_LIST_URL,
            }
        return empty
    except Exception:
        return empty

# ----------------------------------------------------------------------
# 일본 NITE CHRIP(화학물질종합정보제공시스템) — 등재 여부·기본정보 확인
#   GHS분류결과 등 세부 카테고리는 검색 화면의 체크박스 선택에 따라 표시 항목이
#   달라지고 내용이 매우 방대해, 여기서는 등재 여부·CHRIP_ID·일본어/영어명·분자식만
#   확인하고 상세 페이지로 바로 연결되는 링크를 제공한다.
#   세션 쿠키 기반 GET 검색(구식 JSP)이라 사이트 개편 시 이 함수만 고치면 된다.
# ----------------------------------------------------------------------
NITE_SEARCH_URL = "https://www.chem-info.nite.go.jp/chem/chrip/chrip_search/srhInput"

async def fetch_nite_chrip(cas: str) -> dict:
    from bs4 import BeautifulSoup
    empty = {"found": False, "record_url": NITE_SEARCH_URL}
    if not cas:
        return empty
    params = {
        "shMd": "0", "hdUpScPh": "", "cidLt": "",
        "hdInitLtNumMh": "0", "hdInitLtNmMh": "1", "hdInitLtMlMh": "0", "hdInitLtPgCtSt": "100",
        "hdInitRbDp": "0", "hdInitLtScTp": "1", "hdInitRbScMh": "1",
        "txNumSh": cas, "ltNumTp": "1", "ltNumMh": "0",
        "txNmSh": "", "ltNmTp": "", "ltNmMh": "1",
        "txMlSh": "", "ltMlMh": "0", "ltScDp": "0", "ltPgCtSt": "100", "rbDp": "0",
        "_e_srh": "",
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as client:
            await client.get(NITE_SEARCH_URL)
            r = await client.get(NITE_SEARCH_URL, params=params)
            if r.status_code != 200 or "cmpInfDsp" not in str(r.url):
                return empty
            soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return empty

    rec = {}
    for tr in soup.select("table.list-table tr"):
        tds = tr.find_all("td")
        for i in range(0, len(tds) - 1, 2):
            label = tds[i].get_text(strip=True)
            value = tds[i + 1].get_text(strip=True)
            if label and label not in rec:
                rec[label] = value
        if len(rec) >= 5:
            break
    if not rec.get("CHRIP_ID"):
        return empty
    return {
        "found": True,
        "chrip_id": rec.get("CHRIP_ID", ""),
        "name_ja": rec.get("日本語名", ""),
        "name_en": rec.get("英語名", ""),
        "formula": rec.get("分子式", ""),
        "record_url": str(r.url),
    }

# ----------------------------------------------------------------------
# 미국 ATSDR(Toxicological Profiles) — 등재 여부·프로파일 링크 확인
#   ATSDR은 H-code식 분류표가 아니라 서술형 독성프로파일(건강영향 전반)을 제공하므로
#   등재 여부와 상세 링크만 확인한다. 고전 ASP.NET WebForms POST 검색.
# ----------------------------------------------------------------------
ATSDR_SEARCH_URL = "https://wwwn.cdc.gov/TSP/substances/ToxSearch.aspx"

async def fetch_atsdr(cas: str) -> dict:
    from bs4 import BeautifulSoup
    empty = {"found": False, "record_url": ATSDR_SEARCH_URL}
    if not cas:
        return empty
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as client:
            r1 = await client.get(ATSDR_SEARCH_URL)
            if r1.status_code != 200:
                return empty
            soup1 = BeautifulSoup(r1.text, "html.parser")
            def field(name):
                el = soup1.find(id=name)
                return el.get("value", "") if el else ""
            data = {
                "__EVENTTARGET": "", "__VIEWSTATE": field("__VIEWSTATE"),
                "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
                "__VIEWSTATEENCRYPTED": field("__VIEWSTATEENCRYPTED"),
                "__EVENTVALIDATION": field("__EVENTVALIDATION"),
                "ctl00$ContentPlaceHolder1$txtSearch": cas,
                "ctl00$ContentPlaceHolder1$btnSearch": "Search",
            }
            r2 = await client.post(ATSDR_SEARCH_URL, data=data)
            if r2.status_code != 200:
                return empty
            soup2 = BeautifulSoup(r2.text, "html.parser")
    except Exception:
        return empty

    link = next((a for a in soup2.find_all("a")
                 if "ToxSubstance.aspx" in (a.get("href") or "") and cas in a.get_text()), None)
    if not link:
        return empty
    from urllib.parse import urljoin
    name = re.sub(r"\s*\([^)]*\)\s*$", "", link.get_text(strip=True)).strip()
    return {"found": True, "name": name, "record_url": urljoin(ATSDR_SEARCH_URL, link["href"])}

# ----------------------------------------------------------------------
# 고용노동부 KOSHA MSDS — 등재 여부(공단 MSDS/법규정보/유해위험성정보 보유 건수) 확인
#   상세 GHS표는 팝업창(AJAX)이라 여기서는 통합검색 결과로 등재 여부만 확인하고
#   상세는 링크로 안내한다. 세션 없는 단순 POST(JSP).
# ----------------------------------------------------------------------
KOSHA_SEARCH_URL = "https://msds.kosha.or.kr/MSDSInfo/kcic/msdssearchAll.do"

async def fetch_kosha(cas: str) -> dict:
    from bs4 import BeautifulSoup
    empty = {"found": False, "record_url": "https://msds.kosha.or.kr/MSDSInfo/"}
    if not cas:
        return empty
    try:
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            r = await client.post(KOSHA_SEARCH_URL, data={"searchKeyword": cas, "chkAgree": "Y"})
            if r.status_code != 200:
                return empty
            soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return empty
    name_el = next((a for a in soup.find_all("a") if cas in soup.get_text() and a.get_text(strip=True)
                     and "getDetail" in (a.get("href") or "")), None)
    if not name_el or cas not in soup.get_text():
        return empty
    return {"found": True, "name": name_el.get_text(strip=True), "record_url": KOSHA_SEARCH_URL}

# ----------------------------------------------------------------------
# 호주 HCIS(Hazardous Chemical Information System) — GHS 분류 실시간 조회
#   Dynamics 365 Power Pages의 공개 OData API(_api/)로 CAS 검색 → 화학물질ID →
#   상세 페이지(서버사이드 렌더링 HTML)에서 유해성분류 표를 파싱한다. 인증 불필요.
# ----------------------------------------------------------------------
HCIS_BASE = "https://hcis.safeworkaustralia.gov.au"

async def fetch_hcis(cas: str) -> dict:
    from bs4 import BeautifulSoup
    from urllib.parse import quote
    empty = {"found": False, "items": [], "record_url": f"{HCIS_BASE}/"}
    if not cas:
        return empty
    filt = f"(contains(swa_chemicalname,'{cas}') or contains(swa_cas,'{cas}')) and swa_isactive eq true"
    search_url = (f"{HCIS_BASE}/_api/swa_hcis_chemicals?$filter={quote(filt)}"
                  f"&$select=swa_hcis_chemicalid,swa_chemicalname,swa_cas&$orderby=swa_chemicalname asc&$top=25")
    try:
        async with httpx.AsyncClient(timeout=20, headers={**UA, "Accept": "application/json"}) as client:
            r1 = await client.get(search_url)
            if r1.status_code != 200:
                return empty
            items = (r1.json() or {}).get("value") or []
            match = next((x for x in items if x.get("swa_cas") == cas), items[0] if items else None)
            if not match:
                return empty
            cid = match.get("swa_hcis_chemicalid")
            detail_url = f"{HCIS_BASE}/chemical/?id={cid}"
            r2 = await client.get(detail_url)
            if r2.status_code != 200:
                return empty
            soup = BeautifulSoup(r2.text, "html.parser")
    except Exception:
        return empty

    rows = []
    table = soup.find(id="hcis-hc-merged")
    if table:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            rows.append({"item": tds[0].get_text(strip=True),
                         "h": tr.get("data-shorthand", "") or tds[1].get_text(strip=True),
                         "text": tds[2].get_text(strip=True)})
    return {"found": bool(rows), "items": rows, "record_url": detail_url}

# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.get("/api/chem")
async def api_chem(q: str = Query(..., min_length=1)):
    q = q.strip()
    q_is_cas = bool(CAS_RE.match(q))
    key = f"chem::{q.lower()}"
    cached = cache_get(key)
    if cached:
        cached["_cache"] = True
        return JSONResponse(cached)

    # 폴백(UVCB 예시) 우선
    fb = FALLBACK.get(q) or FALLBACK.get(q.lower())
    if fb:
        result = dict(fb); result["query"] = q; result["q_is_cas"] = q_is_cas
        fbcas = (result.get("identity") or {}).get("cas") if q_is_cas else None
        fbcas = fbcas or (q if q_is_cas else None)
        if fbcas:
            result["notice"] = korea_notice_lookup(fbcas)
            fb_ident = result.get("identity") or {}
            result["isomer_check"] = isomer_hydrate_check(
                [fb_ident.get("title")] + list(fb_ident.get("other_names") or []), exclude_cas=fbcas)
            result["classification_ref"] = (CLASSIFICATION_REF.get(fbcas) or {}).get("rows", [])
            result["kreach"] = await fetch_kreach(cas=fbcas)
            result["echa_clp"] = await fetch_echa_clp(fbcas)
            result["ntp"] = ntp_lookup(fbcas)
            result["denmark_clp"] = await fetch_denmark_clp(fbcas)
            result["iarc"] = await fetch_iarc_group(fbcas)
            result["nite"] = await fetch_nite_chrip(fbcas)
            result["kosha"] = await fetch_kosha(fbcas)
            result["hcis"] = await fetch_hcis(fbcas)
        else:
            result["notice"] = {"existing": {"is_existing": None}, "review": {"reviewed": False},
                                 "designated": [], "designated_detail": "", "name_query": True}
            result["isomer_check"] = {"matched": False, "candidates": []}
            result["classification_ref"] = []
            result["kreach"] = {"implemented": True, "found": False, "reason": "name_query",
                                 "links": KREACH_LINKS}
            result["echa_clp"] = {"found": False, "harmonised": [], "industry": [], "record_url": None}
            result["ntp"] = {"listed": False}
            result["denmark_clp"] = {"found": False, "items": [], "record_url": DENMARK_CLP_URL}
            result["iarc"] = {"listed": False, "record_url": IARC_LIST_URL}
            result["nite"] = {"found": False, "record_url": NITE_SEARCH_URL}
            result["kosha"] = {"found": False, "record_url": "https://msds.kosha.or.kr/MSDSInfo/"}
            result["hcis"] = {"found": False, "items": [], "record_url": f"{HCIS_BASE}/"}
        result["phys"] = await fetch_phys_props(
            (result.get("identity") or {}).get("cid"), fbcas or "")
        result["keco"] = await fetch_keco_ncis(cas=fbcas or None,
                                                name=None if fbcas else q)
        cache_set(key, result)
        return JSONResponse(result)

    casc = await fetch_cas_cc(q)
    pub = await fetch_pubchem(q)
    identity = merge_identity(casc, pub)
    data = {
        "found": bool(pub.get("found") or casc.get("found")),
        "source": identity.get("source_label", ""),
        "identity": identity,
        "ghs": pub.get("ghs", {}),
        "record_url": pub.get("record_url"),
        "query": q,
        "q_is_cas": q_is_cas,
    }

    # 기존/신규화학물질 판단: 입력값이 CAS No.일 때만 그 CAS로 자동 판단한다.
    # 물질명 입력은 혼합물(A and B 형태) 표기에서 성분 A의 CAS로 오판될 수 있어
    # 자동판단을 하지 않고, 화학물질정보처리시스템에서 직접 확인하도록 안내한다.
    if q_is_cas:
        data["notice"] = korea_notice_lookup(q)
        data["isomer_check"] = isomer_hydrate_check(
            [identity.get("title")] + list(identity.get("other_names") or []), exclude_cas=q)
        data["classification_ref"] = (CLASSIFICATION_REF.get(q) or {}).get("rows", [])
        data["kreach"] = await fetch_kreach(cas=q)
        data["echa_clp"] = await fetch_echa_clp(q)
        data["ntp"] = ntp_lookup(q)
        data["denmark_clp"] = await fetch_denmark_clp(q)
        data["iarc"] = await fetch_iarc_group(q)
        data["nite"] = await fetch_nite_chrip(q)
        data["kosha"] = await fetch_kosha(q)
        data["hcis"] = await fetch_hcis(q)
    else:
        data["notice"] = {"existing": {"is_existing": None}, "review": {"reviewed": False},
                           "designated": [], "designated_detail": "", "name_query": True}
        data["isomer_check"] = {"matched": False, "candidates": []}
        data["classification_ref"] = []
        data["kreach"] = {"implemented": True, "found": False, "reason": "name_query",
                           "links": KREACH_LINKS}
        data["echa_clp"] = {"found": False, "harmonised": [], "industry": [], "record_url": None}
        data["ntp"] = {"listed": False}
        data["denmark_clp"] = {"found": False, "items": [], "record_url": DENMARK_CLP_URL}
        data["iarc"] = {"listed": False, "record_url": IARC_LIST_URL}
        data["nite"] = {"found": False, "record_url": NITE_SEARCH_URL}
        data["kosha"] = {"found": False, "record_url": "https://msds.kosha.or.kr/MSDSInfo/"}
        data["hcis"] = {"found": False, "items": [], "record_url": f"{HCIS_BASE}/"}

    # 물리화학적 특성(옥탄올-물 분배계수·끓는점·증기압)
    # 물질명 검색이어도 PubChem CID만 있으면 조회 가능하므로 분기 밖에서 수행한다.
    data["phys"] = await fetch_phys_props(
        identity.get("cid"), identity.get("cas") or (q if q_is_cas else ""))

    # 한국환경공단 공식 API — CAS가 있으면 CAS로, 없으면 물질명으로 조회
    _keco_cas = identity.get("cas") or (q if q_is_cas else "")
    data["keco"] = await fetch_keco_ncis(cas=_keco_cas or None,
                                          name=None if _keco_cas else q)

    if data.get("found"):
        cache_set(key, data)
    return JSONResponse(data)

# ----------------------------------------------------------------------
# 국내 규제·우려 자동 생성 (Claude) — "자세히 생성" 버튼을 누를 때만 호출
# ----------------------------------------------------------------------
ENRICH_SYSTEM = (
    "당신은 한국 화학물질 규제 전문가입니다. 주어진 물질에 대해 "
    "①화평법상 기존/신규 판단 ②국내외 규제현황 ③국내 우려 이슈·용도·노출경로 를 정리합니다. "
    "반드시 사실에 기반하고, 확실하지 않은 것은 추론임을 명확히 표시하세요. "
    "각 항목에 출처(기관·규정명)를 함께 적으세요. 과장하지 마세요."
)
# 기존화학물질 고시 제3조(기존화학물질 인정) 판단 기준
EXISTING_CRITERIA = (
    "【기존화학물질 고시 제3조(기존화학물질 인정) 판단기준】 다음은 기존화학물질로 본다: "
    "1) 기존화학물질의 수화물 또는 무수물, "
    "2) 모든 성분이 기존화학물질인 반응생성물(분리가 기술적으로 어렵고 그 자체로 유통·취급되는 경우), "
    "3) 기존화학물질의 이성질체, "
    "4) 별표3에 규정된 물질. "
    "또한 별표1(가목)·별표2(나목) 목록에 CAS가 있으면 기존화학물질(KE-No. 부여)."
)
ENRICH_INSTRUCT = """다음 물질에 대해 JSON만 출력하세요(설명·마크다운 금지).

물질명: {name}
CAS: {cas}
알려진 GHS: {ghs}

[서버가 고시 데이터에서 대조한 사실 — 이 값을 최우선 근거로 삼으세요]
{notice_facts}

{criteria}

출력 JSON 스키마:
{{
  "existing_new": {{
    "verdict":"기존화학물질|신규화학물질|판단보류",
    "basis":"판단 근거(별표1/2/3 수록 여부, KE-No., 제3조 각 호 해당 여부 등)",
    "keno":"KE-No. 있으면 기재, 없으면 빈 문자열",
    "source":"기존화학물질 고시(기후에너지환경부) 및 대조 결과"
  }},
  "designated_estimate": [
    {{"label":"제한물질|금지물질|사고대비물질|중점관리물질|유독물질|등록대상기존화학물질|기타","likely":"예|아니오|불확실","basis":"근거","source":"고시명"}}
  ],
  "regulations": [
    {{"region":"EU|국내|미국|일본|기타","body":"규정/기관명","status":"지정 상태","detail":"핵심 내용","source":"출처"}}
  ],
  "domestic": {{
    "issue": ["국내 우려 이슈 1~3개(각각 근거·출처 포함)"],
    "uses": ["주요 용도"],
    "routes": [{{"r":"흡입|피부|경구","d":"설명"}}],
    "tox": ["우려 독성"],
    "source": "전반 출처 및 성격(확인/추론 구분)"
  }},
  "confidence": "high|medium|low",
  "caveat": "검수 시 주의할 점(특히 반응생성물·이성질체·수화물은 제3조 해당 여부를 사람이 확인 필요)"
}}

규칙:
- existing_new.verdict 는 위 [고시 대조 사실]을 최우선으로 판단하세요. 별표에 CAS가 있으면 '기존화학물질'.
- 별표에 없더라도 제3조 각 호(수화물/무수물, 반응생성물, 이성질체)에 해당할 수 있으면 '판단보류'로 하고 basis에 이유를 적으세요. 함부로 '신규'로 단정하지 마세요.
- designated_estimate: [고시 대조 사실]에 이미 확정된 지정은 제외하고, 확정되지 않은 지정물질(제한·금지·사고대비·중점관리·유독물질 등)에 대해서만 '예/아니오/불확실'로 추정하세요. 규제는 틀리면 위험하므로 확실하지 않으면 '불확실'로 하고 반드시 확인 권고를 남기세요.
- 근거가 불명확하면 regulations 를 빈 배열로 두세요. 절대 없는 규제를 지어내지 마세요."""

def _notice_facts_text(notice: dict) -> str:
    if not notice:
        return "고시 대조 데이터 없음(CAS 미확인)."
    ex = notice.get("existing", {}); rv = notice.get("review", {})
    lines = []
    if ex.get("is_existing"):
        lines.append(f"- 기존화학물질 목록 수록: 예 (구분 {ex.get('table')}, 고유번호 {ex.get('keno')}).")
    else:
        lines.append("- 기존화학물질 목록(별표1/2/3)에서 해당 CAS 미검색 → 신규 가능성 있으나 제3조(수화물·반응생성물·이성질체) 해당 여부 확인 필요.")
    if rv.get("reviewed"):
        lines.append(f"- 유해성심사: 심사됨 ({rv.get('category')}, 번호 {rv.get('no')}). 결과: {rv.get('result')}.")
    else:
        lines.append("- 유해성심사결과 고시에서 해당 CAS 미검색 → 심사 이력 미확인.")
    des = notice.get("designated") or []
    if des:
        lines.append("- 파일 대조로 확정된 지정물질: " +
                     ", ".join(f"{h['label']}({h.get('note','')})" for h in des))
    else:
        lines.append("- 파일 대조된 지정물질 없음(제한·사고대비·중점관리 등은 미확인 → 추정 대상).")
    return "\n".join(lines)

def _extract_json(text: str):
    """마크다운 감싸기·앞뒤 잡텍스트·잘림을 견디며 JSON 객체를 최대한 복구."""
    if not text:
        return None
    t = text.strip()
    # 코드펜스 제거
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I).strip()
    t = re.sub(r"\s*```$", "", t).strip()
    # 1) 그대로 파싱
    try:
        return json.loads(t)
    except Exception:
        pass
    # 2) 첫 '{' 부터 마지막 '}' 까지
    i = t.find("{")
    if i == -1:
        return None
    frag = t[i:]
    j = frag.rfind("}")
    if j != -1:
        try:
            return json.loads(frag[:j + 1])
        except Exception:
            pass
    # 3) 잘린 JSON 복구: 여는 괄호를 스택으로 추적해 올바른 짝으로 닫는다(문자열 밖에서만).
    stack = []; in_str = False; esc = False; buf = []
    for ch in frag:
        buf.append(ch)
        if esc:
            esc = False; continue
        if ch == "\\":
            esc = True; continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]":
                if stack:
                    stack.pop()
    s = "".join(buf)
    if in_str:
        s += '"'
    if stack:
        s = s.rstrip().rstrip(",")
        s += "".join(reversed(stack))
    try:
        return json.loads(s)
    except Exception:
        return None

# ----------------------------------------------------------------------
# Gemini 호출 공통 헬퍼 (텍스트·이미지 프롬프트 모두 사용)
#   503(과부하)·429(요청 과다)·500은 구글 쪽 일시적 오류라 짧게 재시도하면
#   대부분 해결되므로 여기서 한 번에 처리한다.
# ----------------------------------------------------------------------
_GEMINI_RETRY_STATUSES = {503, 429, 500}

async def _gemini_call(payload: dict, timeout: int = 60, model: str = None) -> dict:
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "no_api_key",
                "message": "서버에 GEMINI_API_KEY가 설정되지 않았습니다. Render 환경변수에 추가하세요."}
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    url = f"{GEMINI_URL}/{model or GEMINI_MODEL}:generateContent"
    r = None
    try:
        async with httpx.AsyncClient() as client:
            for attempt in range(3):
                r = await client.post(url, headers=headers, json=payload, timeout=timeout)
                if r.status_code not in _GEMINI_RETRY_STATUSES:
                    break
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
    except Exception as e:
        return {"ok": False, "error": "network", "message": f"Gemini 호출 실패: {e}"}
    if r is None or r.status_code != 200:
        code = r.status_code if r is not None else "?"
        friendly = ("Gemini 서버가 일시적으로 과부하 상태입니다(잠시 후 다시 시도해 주세요)."
                    if code in _GEMINI_RETRY_STATUSES else f"Gemini API 오류 {code}")
        return {"ok": False, "error": "api_error", "message": friendly,
                "detail": (r.text[:400] if r is not None else "")}
    return {"ok": True, "data": r.json()}

def _gemini_text(data: dict):
    """generateContent 응답에서 (본문 텍스트, finishReason)을 뽑는다."""
    cand = (data.get("candidates") or [{}])[0]
    finish = cand.get("finishReason", "")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text, finish

async def enrich_with_gemini(name: str, cas: str, ghs_summary: str, notice: dict = None) -> dict:
    prompt = ENRICH_SYSTEM + "\n\n" + ENRICH_INSTRUCT.format(
        name=name, cas=cas, ghs=ghs_summary or "정보 없음",
        notice_facts=_notice_facts_text(notice), criteria=EXISTING_CRITERIA)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",  # JSON만 출력하도록 강제
        },
    }
    call = await _gemini_call(payload)
    if not call.get("ok"):
        return call
    data = call["data"]
    text, finish = _gemini_text(data)
    # 콘텐츠가 비었을 때(안전차단 등) 안내
    if not text:
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            return {"ok": False, "error": "blocked",
                    "message": "AI가 안전상 이유로 응답을 생성하지 않았습니다. 아래 DB로 직접 확인하세요."}
        pf = data.get("promptFeedback", {})
        return {"ok": False, "error": "empty",
                "message": f"AI 응답이 비어 있습니다(사유 {finish or pf.get('blockReason','')}).",
                "raw": json.dumps(data)[:300]}
    parsed = _extract_json(text)
    if parsed is None:
        note = " (응답이 길어 잘렸을 수 있습니다. 다시 시도해 주세요.)" if finish == "MAX_TOKENS" else ""
        return {"ok": False, "error": "parse_error",
                "message": "AI 응답 파싱 실패" + note, "raw": text[:400]}
    return {"ok": True, "data": parsed, "_finish": finish}

# ----------------------------------------------------------------------
# 구조식 이미지 → SMILES 추정 (Gemini 비전) → PubChem 구조검색으로 실제 물질 확인
#   범용 비전 모델의 구조식 판독(OCSR)은 전용 도구보다 정확도가 낮으므로,
#   결과는 항상 'AI 추정'으로 표시하고 사람이 확인한 뒤 조회하도록 한다.
# ----------------------------------------------------------------------
STRUCTURE_PROMPT = """당신은 화학 구조식 판독 전문가입니다. 첨부된 이미지의 화학 구조식을 읽고 JSON만 출력하세요(설명·마크다운 금지).

가장 중요한 목표는 이 물질을 특정할 수 있는 식별자를 찾는 것입니다.
**1순위: CAS 등록번호(CAS No.)**, **2순위: 화합물 영문명**입니다.

출력 JSON 스키마:
{
  "is_structure": true 또는 false,
  "cas": "CAS 등록번호(예: 71-43-2). 확실하지 않으면 빈 문자열",
  "name": "화합물 영문명(예: Benzene). 확실하지 않으면 빈 문자열",
  "molecular_formula": "분자식(예: C6H6). 확실하지 않으면 빈 문자열",
  "confidence": "high|medium|low",
  "notes": "판독 시 주의점(입체화학 불명확, 화질 문제, 일부 치환기 불확실 등)"
}

규칙:
- 이미지가 화학 구조식이 아니면 is_structure를 false로 하고 나머지는 빈 값으로 두세요.
- CAS 번호는 반드시 그 구조에 실제로 대응하는 번호여야 합니다. 확신이 없으면 빈 문자열로 두세요(틀린 CAS는 없느니만 못합니다).
- 물질명은 널리 통용되는 표준 영문명으로 적으세요.
- 절대 추측으로 그럴듯한 CAS 번호나 물질명을 지어내지 마세요. 모르면 빈 문자열입니다.
- SMILES는 요구하지 않습니다. 출력하지 마세요."""

async def analyze_structure_image(image_bytes: bytes, mime_type: str) -> dict:
    import base64
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime_type,
                             "data": base64.b64encode(image_bytes).decode("ascii")}},
            {"text": STRUCTURE_PROMPT},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }
    call = await _gemini_call(payload, timeout=90)
    if not call.get("ok"):
        return call
    text, finish = _gemini_text(call["data"])
    if not text:
        return {"ok": False, "error": "empty",
                "message": f"AI가 이미지를 판독하지 못했습니다(사유 {finish or '불명'})."}
    parsed = _extract_json(text)
    if parsed is None:
        return {"ok": False, "error": "parse_error",
                "message": "AI 응답 파싱 실패", "raw": text[:300]}
    return {"ok": True, "data": parsed}

async def resolve_smiles(smiles: str) -> dict:
    """SMILES로 PubChem 구조검색 → CID·CAS·분자식 등 실제 등록 정보를 확인."""
    empty = {"found": False}
    smiles = (smiles or "").strip()
    if not smiles:
        return empty
    try:
        async with httpx.AsyncClient(timeout=25, headers=UA) as client:
            # SMILES에 +, # 등 URL 특수문자가 많아 POST(form)로 보낸다.
            r = await client.post(f"{PC}/compound/smiles/cids/JSON", data={"smiles": smiles})
            if r.status_code != 200:
                return {**empty, "reason": f"pubchem_{r.status_code}"}
            cids = (r.json() or {}).get("IdentifierList", {}).get("CID", [])
            if not cids:
                return {**empty, "reason": "no_match"}
            cid = cids[0]
            prop_url = (f"{PC}/compound/cid/{cid}/property/"
                        "MolecularFormula,MolecularWeight,IUPACName,Title,InChIKey,IsomericSMILES/JSON")
            pr, sr = await asyncio.gather(
                client.get(prop_url), client.get(f"{PC}/compound/cid/{cid}/synonyms/JSON"),
                return_exceptions=True)
            p = {}
            if not isinstance(pr, Exception) and pr.status_code == 200:
                props = (pr.json() or {}).get("PropertyTable", {}).get("Properties", [])
                p = props[0] if props else {}
            cas = None
            if not isinstance(sr, Exception) and sr.status_code == 200:
                info = (sr.json() or {}).get("InformationList", {}).get("Information", [])
                syns = info[0].get("Synonym", []) if info else []
                cas = next((s for s in syns if CAS_RE.match(s)), None)
    except Exception as e:
        return {**empty, "reason": f"error:{e}"}
    return {
        "found": True, "cid": cid, "cas": cas,
        "title": p.get("Title"), "formula": p.get("MolecularFormula"),
        "mw": p.get("MolecularWeight"), "iupac": p.get("IUPACName"),
        "inchikey": p.get("InChIKey"), "smiles": p.get("IsomericSMILES") or smiles,
        "structure_url": f"{PC}/compound/cid/{cid}/PNG?image_size=300x300",
        "record_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
    }

@app.post("/api/structure")
async def api_structure(file: UploadFile = File(...)):
    """구조식 이미지를 받아 SMILES를 추정하고 PubChem에서 실제 물질을 대조한다."""
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "message": "빈 파일입니다."}, status_code=400)
    if len(raw) > 8 * 1024 * 1024:
        return JSONResponse({"ok": False, "message": "이미지가 너무 큽니다(최대 8MB)."}, status_code=400)
    mime = (file.content_type or "image/png")
    if not mime.startswith("image/"):
        return JSONResponse({"ok": False, "message": "이미지 파일만 업로드할 수 있습니다."}, status_code=400)

    # 같은 이미지를 다시 올리면 AI 호출 없이 캐시로 즉시 응답(부하·대기시간 절감)
    img_key = "struct::" + hashlib.sha1(raw).hexdigest()
    cached = cache_get(img_key)
    if cached:
        cached["_cache"] = True
        return JSONResponse(cached)

    ai = await analyze_structure_image(raw, mime)
    if not ai.get("ok"):
        return JSONResponse(ai)
    d = ai["data"] or {}
    if not d.get("is_structure"):
        return JSONResponse({"ok": False, "error": "not_structure",
                             "message": "이미지에서 화학 구조식을 찾지 못했습니다.", "ai": d})
    # 대조 우선순위: ① AI가 읽은 CAS No. ② AI가 읽은 물질명
    async def _by_query(q, via):
        got = await fetch_pubchem(q)
        if not got.get("found"):
            return None
        ident = got.get("identity", {})
        return {"found": True, "cid": ident.get("cid"), "cas": ident.get("cas"),
                "title": ident.get("title"), "formula": ident.get("formula"),
                "mw": ident.get("mw"), "iupac": ident.get("iupac"),
                "inchikey": ident.get("inchikey"), "smiles": ident.get("smiles"),
                "structure_url": ident.get("structure_url"),
                "record_url": got.get("record_url"), "via": via}

    resolved = None
    ai_cas = (d.get("cas") or "").strip()
    if CAS_RE.match(ai_cas):
        resolved = await _by_query(ai_cas, "cas")
    if not resolved and (d.get("name") or "").strip():
        resolved = await _by_query(d["name"].strip(), "name")
    if not resolved:
        resolved = {"found": False, "reason": "no_match"}
    result = {"ok": True, "ai": d, "resolved": resolved}
    cache_set(img_key, result)
    return JSONResponse(result)

@app.get("/api/resolve-smiles")
async def api_resolve_smiles(smiles: str = Query(..., min_length=1)):
    """구조식 편집기(JSME)에서 그린 분자의 SMILES로 실제 물질을 대조한다."""
    return JSONResponse({"ok": True, "resolved": await resolve_smiles(smiles)})

@app.get("/api/enrich")
async def api_enrich(q: str = Query(..., min_length=1)):
    q = q.strip()
    key = f"enrich::{q.lower()}"
    cached = cache_get(key)
    if cached:
        cached["_cache"] = True
        return JSONResponse(cached)

    # 식별정보/GHS를 먼저 확보(캐시된 chem 결과 재사용)
    chem = cache_get(f"chem::{q.lower()}")
    if not chem:
        fb = FALLBACK.get(q) or FALLBACK.get(q.lower())
        chem = fb if fb else await fetch_pubchem(q)
    ident = chem.get("identity", {}) if chem else {}
    name = ident.get("title") or q
    cas  = ident.get("cas") or (q if CAS_RE.match(q) else "")
    ghs  = chem.get("ghs", {}) if chem else {}
    ghs_summary = ", ".join(s.get("h", "") for s in ghs.get("statements", [])) or ""

    notice = korea_notice_lookup(cas) if cas else None
    result = await enrich_with_gemini(name, cas, ghs_summary, notice)
    result["query"] = q
    if notice:
        result["notice"] = notice   # 프런트에서 사실 근거로 함께 표시
    if result.get("ok"):
        cache_set(key, result)
    return JSONResponse(result)

# ----------------------------------------------------------------------
# 총칭명 검토 (화평법 자료보호 신청)
#   입력: 화학물질명·총칭명(필수), 구조식 그림/PDF·CAS No.·비고(선택)
#   ① 자동 형식점검(generic_rules, AI 불필요) → ② Gemini 로 구조·명칭·총칭명 대조
#   기준: [별표 1] 총칭명의 명명방법 + Lexicon guide (app/data/generic/rules.md)
# ----------------------------------------------------------------------
GENERIC_FILE_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
GENERIC_MAX_BYTES  = 15 * 1024 * 1024   # Gemini 인라인 요청 한도(20MB)를 넘지 않도록

@app.post("/api/generic-review")
async def api_generic_review(
    chemical_name: str = Form(...),
    generic_name: str = Form(...),
    cas_no: str = Form(""),
    note: str = Form(""),
    use_ai: bool = Form(True),
    use_pubchem: bool = Form(False),
    structure_files: list[UploadFile] = File(default=[]),
):
    chemical_name, generic_name, cas_no = chemical_name.strip(), generic_name.strip(), cas_no.strip()
    if not chemical_name or not generic_name:
        return JSONResponse({"ok": False, "message": "화학물질명과 총칭명은 필수 입력값입니다."}, status_code=400)

    files, total = [], 0
    for up in structure_files:
        raw = await up.read()
        if not raw:
            continue
        mime = up.content_type or ""
        if mime not in GENERIC_FILE_TYPES:
            return JSONResponse({"ok": False, "message": f"{up.filename}: 지원하지 않는 형식입니다(PNG·JPG·WEBP·PDF)."},
                                status_code=400)
        total += len(raw)
        files.append((up.filename or "file", mime, raw))
    if total > GENERIC_MAX_BYTES:
        return JSONResponse({"ok": False, "message": "첨부파일 합계가 15MB를 넘습니다. 구조식이 있는 쪽만 올려 주세요."},
                            status_code=400)

    local = generic_rules.run(chemical_name, generic_name)
    result = {"ok": True,
              "input": {"chemical_name": chemical_name, "generic_name": generic_name, "cas_no": cas_no,
                        "files": [f[0] for f in files]},
              "local": local, "pubchem": None, "ai": None, "ai_error": None}

    if use_pubchem:
        async def _pc(q):
            got = await fetch_pubchem(q)
            if not got.get("found"):
                return {"query": q, "found": False}
            ident = got.get("identity", {})
            return {"query": q, "found": True, "cid": ident.get("cid"), "cas": ident.get("cas"),
                    "title": ident.get("title"), "iupac_name": ident.get("iupac"),
                    "formula": ident.get("formula"), "smiles": ident.get("smiles")}
        result["pubchem"] = {"by_name": await _pc(chemical_name),
                             "by_cas": await _pc(cas_no) if CAS_RE.match(cas_no) else None}

    if use_ai:
        payload = generic_review.build_payload(files, chemical_name, generic_name, cas_no, note.strip(),
                                               local, result["pubchem"])
        call = await _gemini_call(payload, timeout=240, model=GENERIC_REVIEW_MODEL)

        # 2.5 계열은 '생각(thinking)' 토큰도 maxOutputTokens 예산에서 쓴다. 검토 항목이 많아
        # 생각만으로 예산을 다 쓰면 본문이 비고 finishReason=MAX_TOKENS 가 된다.
        # 그 경우에 한해 생각을 끄고 한 번만 다시 시도한다(지원하지 않는 모델이면 원래 오류를 유지).
        if call.get("ok"):
            _text, _finish = _gemini_text(call["data"])
            if not _text and _finish == "MAX_TOKENS":
                retry_payload = json.loads(json.dumps(payload))
                retry_payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
                retry = await _gemini_call(retry_payload, timeout=240, model=GENERIC_REVIEW_MODEL)
                if retry.get("ok") and _gemini_text(retry["data"])[0]:
                    call = retry

        if not call.get("ok"):
            if call.get("error") == "no_api_key":
                # 키 자체가 없으면 Gemini를 호출조차 하지 않는다. 어디를 고쳐야 하는지까지 알려준다.
                result["ai_error"] = ("서버에 GEMINI_API_KEY가 설정되어 있지 않아 Gemini를 호출하지 않았습니다. "
                                      "이 경우 2·5번 'AI 자동 생성'도 같은 이유로 동작하지 않습니다. "
                                      "지금 접속한 서버의 /api/health 를 열어 api_key_set 이 false 인지 확인한 뒤, "
                                      "Render 대시보드 → Environment → GEMINI_API_KEY 를 등록하세요"
                                      "(로컬 실행 중이라면 실행 전에 환경변수를 설정해야 합니다).")
            else:
                # 구글이 돌려준 본문(detail)까지 함께 보여줘야 원인을 알 수 있다.
                detail = (call.get("detail") or "").strip()
                result["ai_error"] = ((call.get("message") or "AI 호출 실패")
                                      + (f" · 응답: {detail[:300]}" if detail else ""))
        else:
            text, finish = _gemini_text(call["data"])
            parsed = _extract_json(text)
            if not isinstance(parsed, dict) or finish == "MAX_TOKENS":
                if text:
                    result["ai_error"] = (f"AI 응답을 해석하지 못했습니다(사유 {finish or '불명'}). 다시 시도해 주세요."
                                          + (" 응답이 길이 제한에 걸려 잘렸습니다." if finish == "MAX_TOKENS" else ""))
                else:
                    pf = (call["data"].get("promptFeedback") or {}).get("blockReason", "")
                    result["ai_error"] = (f"AI가 응답을 생성하지 않았습니다(사유 {finish or pf or '불명'})."
                                          + (" 첨부 구조식 없이 명칭만으로 다시 시도해 보세요."
                                             if finish == "MAX_TOKENS" else ""))
            else:
                result["ai"] = generic_review.normalize(parsed)
                result["ai"]["_model"] = GENERIC_REVIEW_MODEL

    result["verdict"], result["verdict_note"] = generic_review.final_verdict(local, result["ai"])
    return JSONResponse(result)

@app.get("/api/health")
async def health():
    return {"ok": True, "api_key_set": bool(GEMINI_API_KEY), "model": GEMINI_MODEL}

# 정적 콘솔(UI)을 루트에 마운트 — API 라우트 뒤에 둬야 함
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
