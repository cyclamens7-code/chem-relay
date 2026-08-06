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
from fastapi import FastAPI, Query, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import httpx, json, re, hashlib, time, pathlib, asyncio, os, secrets

# ── Gemini(Google) 설정 ─────────────────────────────────────────────
# Google AI Studio 무료 API 키를 서버 환경변수로 주입 → 브라우저에 노출 안 됨.
# 무료 티어(카드 불필요)로 "자세히 생성"을 돌립니다.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/models"

# ── CAS Common Chemistry API 설정 ──────────────────────────────────
# 참고자료 우선순위: 1) CAS Common Chemistry  2) PubChem
# https://www.cas.org/services/commonchemistry-api 에서 API 키를 발급받아
# CAS_CC_API_KEY 환경변수로 주입하면 자동으로 1순위 출처가 됩니다.
# 키가 없으면 자동으로 PubChem(2순위)만 사용합니다(기존 동작과 동일).
CAS_CC_API_KEY = os.getenv("CAS_CC_API_KEY", "")
CAS_CC_BASE    = "https://commonchemistry.cas.org/api"

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
# 이성질체·무수물·수화물 매칭 (CAS 기준 기존화학물질 판단 보완용)
#   입력 CAS로 유추한 물질명에서 cis/trans/(E)/(Z) 같은 이성질체 표기와
#   수화물·무수물 표기를 제거한 뒤, 기존화학물질 목록의 물질명과 대조한다.
#   매칭되면(다른 CAS인데 같은 base name) 고시 제3조(이성질체·수화물·무수물)
#   해당 가능성이 있으므로 화학물질정보처리시스템에서 반드시 확인하도록 안내한다.
# ----------------------------------------------------------------------
_ISOMER_PREFIX_RE = re.compile(r"(?i)\b(cis|trans)\b[-\s]*|\(\s*[EZRS]\s*\)[-\s]*")
_HYDRATE_RE = re.compile(
    r"(?i)\b(mono|di|tri|tetra|penta|hexa|hepta|octa|nona|deca)?\s*hydrate\b|\banhydrous\b")

def _normalize_isomer_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower()
    s = _ISOMER_PREFIX_RE.sub(" ", s)
    s = _HYDRATE_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s

def _build_name_index():
    idx = {}
    for cas, rec in EXISTING_CHEM.items():
        key = _normalize_isomer_name(rec.get("name") or "")
        if not key:
            continue
        idx.setdefault(key, []).append({
            "cas": cas, "name": rec.get("name"),
            "keno": rec.get("keno"), "table": rec.get("table"),
        })
    return idx
EXISTING_NAME_INDEX = _build_name_index()

def isomer_hydrate_check(name: str, exclude_cas: str = None) -> dict:
    key = _normalize_isomer_name(name)
    if not key:
        return {"matched": False, "candidates": []}
    hits = [h for h in EXISTING_NAME_INDEX.get(key, []) if h["cas"] != exclude_cas]
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
        trs = body.find_all("tr")
        if "유해성" in cap_text:
            for tr in trs:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 6 or "없습니다" in "".join(tds):
                    continue
                haz_rows.append({"tab": tab_label, "item": tds[0], "grade": tds[1],
                                  "picto": tds[2], "signal": tds[3], "hcode_text": tds[4],
                                  "reason": tds[5] if len(tds) > 5 else ""})
        elif "용도" in cap_text:
            for tr in trs:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 2 or "없습니다" in "".join(tds):
                    continue
                use_rows.append({"tab": tab_label, "no": tds[0], "use": tds[1]})
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
            result["isomer_check"] = isomer_hydrate_check(
                (result.get("identity") or {}).get("title"), exclude_cas=fbcas)
            result["classification_ref"] = (CLASSIFICATION_REF.get(fbcas) or {}).get("rows", [])
            result["kreach"] = await fetch_kreach(cas=fbcas)
            result["echa_clp"] = await fetch_echa_clp(fbcas)
            result["ntp"] = ntp_lookup(fbcas)
            result["denmark_clp"] = await fetch_denmark_clp(fbcas)
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
        data["isomer_check"] = isomer_hydrate_check(identity.get("title"), exclude_cas=q)
        data["classification_ref"] = (CLASSIFICATION_REF.get(q) or {}).get("rows", [])
        data["kreach"] = await fetch_kreach(cas=q)
        data["echa_clp"] = await fetch_echa_clp(q)
        data["ntp"] = ntp_lookup(q)
        data["denmark_clp"] = await fetch_denmark_clp(q)
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

async def enrich_with_gemini(name: str, cas: str, ghs_summary: str, notice: dict = None) -> dict:
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "no_api_key",
                "message": "서버에 GEMINI_API_KEY가 설정되지 않았습니다. Render 환경변수에 추가하세요."}
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
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    url = f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            return {"ok": False, "error": "api_error",
                    "message": f"Gemini API 오류 {r.status_code}", "detail": r.text[:400]}
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        finish = cand.get("finishReason", "")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
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

@app.get("/api/health")
async def health():
    return {"ok": True, "api_key_set": bool(GEMINI_API_KEY), "model": GEMINI_MODEL}

# 정적 콘솔(UI)을 루트에 마운트 — API 라우트 뒤에 둬야 함
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
