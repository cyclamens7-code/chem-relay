# 화학물질 유해성 조회 콘솔 (Render 배포)

**하나의 웹 서비스**가 웹 콘솔(UI)과 중계 서버(API)를 함께 제공합니다.
브라우저가 직접 못 부르는 공개 DB(PubChem, CAS Common Chemistry 등)를 서버가 대신 조회하므로 CORS 문제가 없습니다.

- UI  : `https://<render-서비스주소>/`
- API : `https://<render-서비스주소>/api/chem?q=71-43-2`

Render의 **Docker 런타임**으로 배포합니다(`render.yaml` + `Dockerfile`). 별도 서버 관리가 필요 없습니다.

---

## 배포 (Render)

### 1. GitHub 저장소 준비
이 프로젝트를 GitHub에 업로드(이미 완료했다면 생략).
`render.yaml`이 저장소 루트에 있어야 Render가 자동으로 인식합니다.

### 2. Render에서 서비스 생성
1. https://dashboard.render.com → **New → Blueprint**
2. GitHub 저장소 연결 → `render.yaml`을 자동 인식(서비스명 `chem-relay`, `runtime: docker`, `plan: free`)
3. **Apply** → 첫 빌드가 진행됩니다(수 분 소요).

### 3. 환경변수(API 키) 입력 — 대시보드에서 직접
`render.yaml`에 `sync: false`로 되어 있는 키는 저장소에 값이 저장되지 않으므로,
Render 대시보드 → 서비스 → **Environment** 탭에서 직접 입력해야 합니다.

| 키 | 필수 | 용도 |
|---|---|---|
| `GEMINI_API_KEY` | 선택 | 2·5번 "자세히 생성"(국내외 규제현황·우려이슈)과 구조식 이미지 판독 — 없으면 해당 기능만 비활성, 나머지는 무료로 동작 |
| `CAS_CC_API_KEY` | 선택 | 1·3번 기본정보 1순위 출처(CAS Common Chemistry) — 없으면 자동으로 2순위(PubChem)만 사용 |
| `KECO_API_KEY` | 선택 | 1번 기본정보의 **한국환경공단 공식 API**(KE번호·국문명·물질분류) — 없으면 해당 블록만 숨겨짐 |
| `KECO_NCIS_URL` | 선택 | 위 API 엔드포인트. 기본값 `https://apis.data.go.kr/B552584/kecoapi/ncissbstn`. 포털에서 주소가 바뀌면 **코드 수정 없이** 이 값만 교체 |
| `KECO_NCIS_PARAM` | 선택 | CAS 검색 파라미터명. 비워두면 `casNo` 등 후보를 자동 탐색하므로 보통 설정 불필요 |

값을 입력하고 저장하면 Render가 자동으로 재배포합니다.

### 4. 접속
Render 대시보드에 표시되는 서비스 URL(`https://chem-relay-xxxx.onrender.com` 형태)로 접속합니다.
무료 플랜은 트래픽이 없으면 슬립되므로, 첫 요청 시 기동에 수십 초가 걸릴 수 있습니다.

---

## 폴더 구조
```
├─ render.yaml             # Render 배포 정의(Blueprint)
├─ Dockerfile               # python:3.12-slim
├─ requirements.txt
├─ scripts/
│  └─ build_classification_reference.py   # 물질별 엑셀 → classification_reference.json 추출
└─ app/
   ├─ main.py               # FastAPI: /api/chem, /api/enrich + UI 서빙
   ├─ data/                 # 기존화학물질·유해성심사·지정물질·국외 분류표시 등 내장 DB(JSON)
   └─ static/
      └─ index.html         # 웹 콘솔
```

---

## 지금 자동으로 되는 것 / 확장 지점

| 항목 | 상태 | 비고 |
|---|---|---|
| 기본정보·이명·구조식·화학식 | ✅ 자동 | 우선순위 1) CAS Common Chemistry 2) PubChem (아래 참고) |
| 구조식으로 검색 — 이미지 판독 | 🤖 Gemini(선택) | 구조식 이미지 업로드 → Gemini 비전이 SMILES 추정 → PubChem 구조검색으로 실제 물질 대조 → CAS로 전체 조회. `analyze_structure_image()` / `POST /api/structure` |
| 구조식으로 검색 — 직접 그리기 | ✅ 자동 | 무료 오픈소스 편집기 **JSME**(jsdelivr CDN)로 그린 구조 → SMILES → PubChem 대조. AI 불필요. `GET /api/resolve-smiles` |
| 물리화학적 특성(옥탄올-물 분배계수·끓는점·증기압) | ✅ 자동 | 1순위 PubChem 실험값(HSDB·ICSC 등 원출처 표기), 없는 항목만 2순위 **EPA EPI Suite** 공개 API로 보완. `fetch_phys_props()` |
| 기존/신규화학물질 판단 | ✅ 자동(CAS 입력 시만) | CAS No. 입력일 때만 판단. 물질명 입력은 혼합물 오판 방지를 위해 자동판단하지 않고 확인 링크만 제공 |
| GHS 분류(H코드·픽토그램·출처) | ✅ 자동 | 국내 유해성심사 고시 + EU CLP(실시간) + US NTP(고정목록) + 물질별 제출 자료('참고 분류표시' 시트) |
| 유해성 요약 | ✅ 자동 | 국내 유해성심사 + EU CLH(조화분류, 실시간 우선)만 근거로 사용 |
| 화학물질정보처리시스템(KREACH) 신고·등록 유해성분류·용도 | ✅ 자동(스크래핑) | 공개 API가 없어 검색→상세 폼 POST를 재현. 사이트 개편 시 `fetch_kreach()`만 수정 |
| EU CLP(ECHA CHEM) 조화분류·산업계 신고분류 | ✅ 자동(실시간 API) | `chem.echa.europa.eu`의 공개 REST 엔드포인트 사용(로그인·키 불필요). `fetch_echa_clp()` |
| US NTP 발암성 보고서(RoC) 등재 여부 | ✅ 자동(고정목록) | 15차 보고서(2021) CASRN 인덱스를 미리 추출(`app/data/ntp_roc.json`). 신규 발간 시 `scripts/build_ntp_roc.py` 재실행 |
| 덴마크 CLP 데이터베이스 | ✅ 자동(실시간 스크래핑) | `clp-vejlliste.mst.dk`의 ASP.NET WebForms 검색(POST)을 재현. 사이트 개편 시 `fetch_denmark_clp()`만 수정 |
| IARC Monographs 발암성 분류(Group 1~4) | ✅ 자동(실시간) | 목록 페이지가 불러오는 JS 번들에 전체 데이터가 내장되어 있어 매 조회마다 새로 받아 파싱. `fetch_iarc_group()` |
| 일본 NITE CHRIP 등재 확인(CHRIP_ID·일본어/영어명·분자식) | ✅ 자동(실시간 스크래핑) | 세션 쿠키 기반 GET 검색(구식 JSP)을 재현. GHS분류결과 등 세부 항목은 화면 카테고리 체크박스에 따라 표시가 크게 달라져 상세 링크만 제공. `fetch_nite_chrip()` |
| 미국 ATSDR 등재 확인(독성프로파일 링크) | ✅ 자동(실시간 스크래핑) | ASP.NET WebForms POST 검색 재현. 서술형 독성프로파일이라 H-code표 대신 등재 여부·상세 링크 제공. `fetch_atsdr()` |
| 고용노동부 MSDS(KOSHA) 등재 확인 | ✅ 자동(실시간 스크래핑) | 세션 없는 단순 POST 검색. 상세 GHS표는 팝업(AJAX)이라 등재 여부·링크만 제공. `fetch_kosha()` |
| 호주 HCIS GHS 분류 | ✅ 자동(실시간 API) | Power Pages 공개 OData API(`_api/`)로 CAS 검색 → 상세 페이지 파싱. 인증 불필요. `fetch_hcis()` |
| EU RAR(ESR 위해성평가), ECHA Biocidal | 🔧 링크만 | `echa.europa.eu` 메인 도메인 전체가 Azure WAF(봇 차단)로 막혀 있어 서버 간 조회가 불가능. 봇 차단 우회는 하지 않으므로 DB 링크만 제공 |
| US EPA RED(농약 재등록결정) | 🔧 링크만 | Oracle APEX 기반이라 검색 제출 토큰이 JS로 동적 계산되어 서버 간 재현이 불안정. 농약 등록 정보라 일반 산업화학물질과의 관련성도 낮아 링크만 제공 |
| 호주 AICIS, OECD SIDS, 소방청, US EPA IRIS, EFSA, 뉴질랜드 CCID, 식약처 발암성정보 | 🔧 링크만 | Dynamics CRM 메타데이터 응답이 매우 복잡하거나(AICIS), 검색 제출이 안정적으로 재현되지 않거나(OECD SIDS·소방청), 아직 확인하지 못한 경우(EPA IRIS·EFSA·NZ CCID·식약처) — 추후 필요 시 순서대로 이어서 구현 예정 |
| 국내외 규제현황·우려이슈 "자세히 생성" | 🤖 Gemini(선택) | 아래 참고 |
| 조회 캐시 | ✅ | 30일, `CACHE_DIR`(기본 `/tmp/cache`) |

### 참고자료 우선순위 — CAS Common Chemistry API

기본정보(물질명·이명·CAS·분자식·SMILES·InChIKey)는 **1) CAS Common Chemistry, 2) PubChem** 순서로 조회하고,
어느 쪽 자료인지 화면에 표시합니다. CAS Common Chemistry는 API 키가 있어야 조회되며,
키가 없으면 자동으로 2순위(PubChem)만 사용합니다.

1. https://www.cas.org/services/commonchemistry-api 에서 API 키 신청
2. Render 대시보드 → Environment → `CAS_CC_API_KEY`에 키 입력 (위 "배포" 3번 참고)

### 2·5번 자동 생성 (하이브리드) — Gemini API 연결

콘솔에서 **국내외 규제현황(2번)** · **국내 우려·용도·노출경로(5번)** 항목은
서버가 Google Gemini를 호출해 출처와 함께 초안을 정리합니다(`app/main.py`의 `enrich_with_gemini()`).
조회 시 자동 호출되며, 결과는 캐시에 저장돼 같은 물질 재조회는 추가 호출이 없습니다.

**설정 방법**
1. https://aistudio.google.com 에서 무료 API 키 발급(카드 불필요)
2. Render 대시보드 → Environment → `GEMINI_API_KEY`에 키 입력
3. 기본 모델은 `gemini-flash-latest`이며, `GEMINI_MODEL` 환경변수로 교체 가능합니다.
4. 키가 없으면 "자세히 생성" 관련 항목만 비활성이고, 나머지(기본정보·GHS·유해성 요약 등)는 그대로 무료로 동작합니다.

**중요 — 검수 전제**
- Gemini가 만든 규제·우려 내용은 **초안**입니다. 화면에 신뢰도(high/medium/low)와
  검수 주의사항이 함께 표시되며, 각 항목에 출처를 답니다.
- 규제 정보는 틀리면 문제가 되므로, **게시·활용 전 출처 확인과 사람 검수**를 반드시 하세요.
- API 키는 서버 안에만 있고 브라우저에는 절대 노출되지 않습니다.
- 일시적 오류(503 과부하 / 429 요청과다 / 500)는 서버가 자동으로 최대 3회 재시도합니다.

### 구조식으로 검색 (이미지 판독 / 직접 그리기)

상단 **"⌬ 구조식으로 검색"** 버튼에서 두 가지 방법으로 물질을 찾을 수 있습니다.

| 방법 | 동작 | AI 필요 |
|---|---|---|
| **이미지 업로드** | 구조식 그림(PNG·JPG·WEBP, 최대 8MB)을 올리면 Gemini 비전이 **CAS No.(1순위)·물질명(2순위)**·분자식을 판독 → 그 순서로 PubChem에서 실제 물질 대조 → 확인된 CAS로 전체 조회. (SMILES는 판독 단계에서 요구하지 않음) | ⭕ `GEMINI_API_KEY` |
| **직접 그리기** | 무료 오픈소스 편집기 **JSME**로 구조를 그리면 브라우저에서 SMILES를 만들고 → PubChem 구조검색으로 대조 → CAS로 전체 조회 | ❌ 불필요 |
| **Google Lens / Google AI 모드** | 서버를 전혀 거치지 않고 새 탭에서 직접 확인. Lens는 이미지를 직접 올려 검색, AI 모드(`udm=50`)는 CAS·물질명·분자식·SMILES·InChIKey를 한 번에 묻는 질의로 열림 | ❌ 불필요 |

**API 부하·속도 최적화**
- 업로드 전 **브라우저에서 이미지를 축소**해 보냅니다(긴 변 1200px·JPEG). 실측 471KB → 154KB로 줄어 전송·판독이 모두 빨라집니다.
- **같은 이미지는 서버가 캐시**(SHA-1 해시 키)해 재업로드 시 AI를 호출하지 않습니다. 실패 응답은 캐시하지 않습니다.
- Gemini 호출은 일시적 오류(503/429/500) 시 자동으로 최대 3회 재시도합니다.

**정확도 주의 (중요)**
- 범용 비전 모델의 구조식 판독(OCSR)은 **전용 화학 인식 도구보다 정확도가 낮습니다.**
  벤젠 고리·단순 작용기는 잘 맞히지만, 입체화학(쐐기/점선)·복잡한 고리계·손그림에서는 자주 틀립니다.
- 그래서 화면에 항상 **"AI 판독 결과 — 검수 필요"**와 신뢰도가 함께 뜨고,
  PubChem에서 실제로 대조된 구조식 그림·CAS를 **눈으로 확인한 뒤** 조회하도록 한 단계를 둡니다.
- 판독이 틀린 것 같으면 **직접 그리기** 탭을 쓰면 AI를 거치지 않아 정확합니다.

JSME는 `https://cdn.jsdelivr.net/npm/jsme-editor/jsme.nocache.js` 를 CDN에서 불러옵니다(설치·빌드 불필요).

### 국내 공식 정보 — 한국환경공단 오픈API (공공데이터포털)

[한국환경공단_화학물질 정보 조회 서비스](https://www.data.go.kr/data/15149420/openapi.do)로
**KE번호(고유번호)·물질명(국문/영문)·분자식·분자량·물질분류**를 국내 공식 출처에서 가져와
**1번 화학물질 기본정보**에 표시합니다.

가장 큰 쓸모는 **교차검증**입니다. 이 앱은 평소 내장 고시파일(`app/data/existing_chemicals.json`)로
기존/신규를 판정하는데, 공식 API가 **KE번호를 주는데 내장 파일은 '기존'이 아니라고 본 경우**
불일치 경고를 띄우고 "공식 API 결과를 우선 검토하라"고 안내합니다(내장 고시파일이 최신이 아닐 수 있으므로).

**설정**
1. 공공데이터포털에서 위 서비스에 **활용신청** → 승인
2. Render 대시보드 → Environment → `KECO_API_KEY`에 인증키 입력
3. 키가 없으면 이 블록은 아예 표시되지 않고, 나머지 기능은 그대로 동작합니다.

> **승인 전에는 `NO_OPENAPI_SERVICE_ERROR`가 반환됩니다.** 이는 정상이며 화면에
> "인증키가 아직 이 서비스에 반영되지 않았습니다"로 안내됩니다. 승인이 끝나면 자동으로 표시됩니다.
> 포털에서 엔드포인트 주소나 파라미터명이 다르게 안내되면 `KECO_NCIS_URL` / `KECO_NCIS_PARAM`
> 환경변수만 바꾸면 되고 코드는 고칠 필요가 없습니다(파라미터명은 비워두면 자동 탐색).

### 물리화학적 특성 — PubChem + EPA EPI Suite

**1번 화학물질 기본정보**에 옥탄올-물 분배계수(log Kow)·끓는점·증기압을 표시합니다.

1. **1순위 PubChem 실험값** — PUG-View에서 항목별로 가져오며, HSDB·ICSC·CAMEO 등
   **원출처를 함께 표기**합니다. 값이 여러 개면 대표값 1개 + "다른 출처"로 최대 2개를 더 보여줍니다.
2. **2순위 EPA EPI Suite** — PubChem에 없는 항목만 `https://episuite.dev/api/submit` 공개 API로 보완합니다.
   EPI Suite는 **실측**(문헌 측정값)과 **추정**(모델 예측)을 구분해 주므로 화면에도 배지로 구분 표시하고,
   실측값이 있을 때는 모델 추정값도 함께 보여줍니다.

세 항목을 PubChem에서 모두 찾으면 EPI Suite는 호출하지 않습니다(불필요한 외부 호출 방지).
인증·API 키가 필요 없으며, 사이트 개편 시 `fetch_phys_props()` / `fetch_episuite()`만 고치면 됩니다.

### 국내 DB 스크래핑 — 화학물질정보처리시스템(KREACH)
`app/main.py`의 `fetch_kreach(cas, name)` 함수가 KREACH 정보공개 화면(검색→상세)을
대신 조회해 신고ㆍ등록 정보의 유해성 분류·표시, 용도정보를 가져옵니다(`beautifulsoup4` 사용).
공개 API가 없어 실제 사이트의 검색 폼 POST를 재현하는 방식이라, 정부 사이트 개편 시
`fetch_kreach()`와 `_kreach_parse_tab()`만 고치면 되도록 분리해 뒀습니다.

### 국외 DB 대조 자료('참고 분류표시' 시트) 갱신
물질별 결과물 엑셀(xlsm)의 2번째 시트 '(참고) 분류표시'(국외 DB별 유해성 분류·표시)를
`app/data/classification_reference.json`(CAS 기준)으로 미리 추출해 3번 항목에 표시합니다.
결과물 엑셀이 갱신되면 아래 스크립트를 로컬에서 다시 실행해 JSON을 갱신하고,
바뀐 `app/data/classification_reference.json`을 GitHub에 커밋·푸시하면 Render가 재배포합니다.
```
py scripts/build_classification_reference.py
```

---

## 문제 해결
- **배포 후 접속이 안 됨** → Render 대시보드에서 서비스 상태가 `Live`인지, 빌드/배포 로그(Logs 탭)에 에러가 없는지 확인.
- **첫 요청이 느림** → 무료 플랜은 트래픽이 없으면 슬립되어 재기동에 시간이 걸립니다(정상).
- **조회가 느리거나 실패** → Render 서버가 외부 인터넷에 나갈 수 있어야 합니다(PubChem·CAS Common Chemistry·KREACH 접속 필요). 첫 조회 후에는 캐시로 빨라집니다.
- **"자세히 생성"이 안 됨** → `GEMINI_API_KEY` 환경변수가 비어있는지 확인(Environment 탭).
- **UVCB(석유유분 등)** → PubChem·CAS Common Chemistry에 구조 레코드가 없어 "레코드 없음"으로 표시됩니다.
  이는 정상이며, 해당 물질은 DB 링크로 확인하는 것이 맞습니다.

---

## (참고, 현재 미사용) NAS 자체 호스팅
이 프로젝트는 원래 Synology NAS(Container Manager) 자체 호스팅용으로도 만들어졌으나,
현재는 Render로 운영 중이라 NAS 배포는 사용하지 않습니다. `docker-compose.yml`이
그 흔적으로 남아있으며, 나중에 NAS로 옮길 때 참고할 수 있도록 환경변수만 최신 코드에
맞춰(`GEMINI_*`, `CAS_CC_API_KEY`) 정리해 뒀습니다. Render 배포에는 이 파일이 사용되지 않습니다.
