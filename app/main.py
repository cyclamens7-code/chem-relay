"""
화학물질 유해성 중계 서버 (Synology DS218+ / Container Manager 용)
------------------------------------------------------------------
역할
  1) 브라우저가 직접 못 부르는 공개 DB(PubChem 등)를 서버가 대신 조회 → CORS 우회
  2) 조회 결과를 하나의 JSON으로 정리해서 프런트(콘솔)에 반환
  3) 디스크 캐시로 반복 조회를 빠르게 + PubChem 호출량 절감
  4) 국내 정부 DB(기후부/KOSHA 등) 스크래핑을 붙일 수 있는 자리 마련(fetch_korea_*)

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

# ── Anthropic(Claude) 설정 ──────────────────────────────────────────
# API 키는 서버(NAS) 환경변수로만 주입 → 브라우저에 절대 노출되지 않음.
# docker-compose.yml 의 environment 에서 설정합니다.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

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
                    "MolecularFormula,MolecularWeight,IUPACName,Title/JSON")
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
            syns = info[0].get("Synonyms", []) if info else []
        cas = next((s for s in syns if CAS_RE.match(s)), q if CAS_RE.match(q) else None)

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
                "cas": cas, "ec": None, "ke": None,
                "formula": p.get("MolecularFormula"),
                "mw": p.get("MolecularWeight"),
                "iupac": p.get("IUPACName"),
                "cid": cid,
                "structure_url": f"{PC}/compound/cid/{cid}/PNG?image_size=300x300",
            },
            "ghs": {**ghs,
                    "source_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=GHS-Classification"},
            "record_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        }

# ----------------------------------------------------------------------
# 국내 정부 DB 훅 (스크래핑을 붙일 자리) — 지금은 안내 링크만 반환
# ----------------------------------------------------------------------
async def fetch_korea(cas: str) -> dict:
    """
    기후부(KREACH)/KOSHA MSDS 등은 공개 API가 없어 HTML 스크래핑이 필요합니다.
    사이트 개편 때 깨질 수 있으므로 별도 함수로 분리해 둡니다.
    실제 구현 시: httpx로 검색 요청 → HTML 파싱(BeautifulSoup) → 분류/표시 추출.
    지금은 링크 안내만 반환합니다.
    """
    return {
        "implemented": False,
        "links": {
            "기후부 화학물질정보처리시스템": "https://kreach.mcee.go.kr/repwrt/index.do",
            "고용노동부 MSDS": "https://msds.kosha.or.kr/MSDSInfo/",
            "소방청 국가위험물통합정보": "https://hazmat.nfa.go.kr/",
        },
    }

# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.get("/api/chem")
async def api_chem(q: str = Query(..., min_length=1)):
    q = q.strip()
    key = f"chem::{q.lower()}"
    cached = cache_get(key)
    if cached:
        cached["_cache"] = True
        return JSONResponse(cached)

    # 폴백(UVCB 예시) 우선
    fb = FALLBACK.get(q) or FALLBACK.get(q.lower())
    if fb:
        result = dict(fb); result["query"] = q
        cache_set(key, result)
        return JSONResponse(result)

    data = await fetch_pubchem(q)
    data["query"] = q
    if data.get("found"):
        cas = (data.get("identity") or {}).get("cas") or (q if CAS_RE.match(q) else None)
        if cas:
            data["korea"] = await fetch_korea(cas)
        cache_set(key, data)
    return JSONResponse(data)

# ----------------------------------------------------------------------
# 국내 규제·우려 자동 생성 (Claude) — "자세히 생성" 버튼을 누를 때만 호출
# ----------------------------------------------------------------------
ENRICH_SYSTEM = (
    "당신은 한국 화학물질 규제 전문가입니다. 주어진 물질에 대해 "
    "①국내외 규제현황 ②국내 우려 이슈·용도·노출경로 를 정리합니다. "
    "반드시 사실에 기반하고, 확실하지 않은 것은 추론임을 명확히 표시하세요. "
    "각 항목에 출처(기관·규정명)를 함께 적으세요. 과장하지 마세요."
)
ENRICH_INSTRUCT = """다음 물질에 대해 JSON만 출력하세요(설명·마크다운 금지).

물질명: {name}
CAS: {cas}
알려진 GHS: {ghs}

출력 JSON 스키마:
{{
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
  "caveat": "검수 시 주의할 점"
}}

규칙:
- 근거가 불명확하면 regulations 를 빈 배열로 두고 domestic.issue 에 '공개 자료에서 규제현황 확인 필요'라고 적으세요.
- 절대 없는 규제를 지어내지 마세요. 확실한 것만."""

async def enrich_with_claude(name: str, cas: str, ghs_summary: str) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "no_api_key",
                "message": "서버에 ANTHROPIC_API_KEY가 설정되지 않았습니다. docker-compose.yml 참고."}
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "system": ENRICH_SYSTEM,
        "messages": [{"role": "user",
                      "content": ENRICH_INSTRUCT.format(name=name, cas=cas, ghs=ghs_summary or "정보 없음")}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            return {"ok": False, "error": "api_error",
                    "message": f"Anthropic API 오류 {r.status_code}", "detail": r.text[:400]}
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return {"ok": False, "error": "parse_error", "message": "응답 파싱 실패", "raw": text[:400]}
        return {"ok": True, "data": parsed}

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

    result = await enrich_with_claude(name, cas, ghs_summary)
    result["query"] = q
    if result.get("ok"):
        cache_set(key, result)
    return JSONResponse(result)

@app.get("/api/health")
async def health():
    return {"ok": True, "api_key_set": bool(ANTHROPIC_API_KEY), "model": ANTHROPIC_MODEL}

# 정적 콘솔(UI)을 루트에 마운트 — API 라우트 뒤에 둬야 함
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
