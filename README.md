# 화학물질 유해성 조회 — NAS 중계 서버 (Synology DS218+)

컨테이너 **하나**가 웹 콘솔(UI)과 중계 서버(API)를 함께 제공합니다.
브라우저가 직접 못 부르는 공개 DB(PubChem 등)를 서버가 대신 조회하므로 CORS 문제가 없습니다.

- UI  : `http://<NAS_IP>:8000/`
- API : `http://<NAS_IP>:8000/api/chem?q=71-43-2`

DS218+ 는 x86(Intel Celeron J3355) 모델이라 Container Manager를 지원합니다. RAM 2GB로 충분합니다.

---

## 설치 (Container Manager, 프로젝트 방식 — 가장 쉬움)

### 1. 사전 준비
- DSM을 **7.2 이상**으로 업데이트 → 패키지 센터에서 **Container Manager** 설치
- File Station에서 공유폴더 하나에 폴더 생성: 예) `docker/chem-relay`
- 이 폴더 안에 이 프로젝트 파일을 **통째로 업로드**
  (`Dockerfile`, `docker-compose.yml`, `requirements.txt`, `app/` 폴더 전체)

### 2. 프로젝트 생성
1. **Container Manager → 프로젝트 → 생성**
2. 프로젝트 이름: `chem-relay`
3. 경로: 방금 업로드한 `docker/chem-relay` 폴더 선택
4. 소스: **기존 docker-compose.yml 사용** 선택 (자동으로 인식됨)
5. **다음 → 완료.** 첫 실행 시 이미지를 빌드하므로 몇 분 걸립니다.

### 3. 접속
- 같은 네트워크 PC 브라우저에서 `http://<NAS_IP>:8000/`
- NAS_IP는 DSM → 제어판 → 정보센터 → 네트워크에서 확인
- 8000 포트가 다른 서비스와 겹치면 `docker-compose.yml`의 `"8000:8000"`을
  `"9000:8000"` 처럼 **왼쪽 숫자만** 바꾸세요.

---

## 외부(집 밖)에서도 쓰려면
- DSM **역방향 프록시**(제어판 → 로그인 포털 → 고급 → 역방향 프록시)로
  `chem.내도메인` → `localhost:8000` 연결 + Let's Encrypt 인증서 적용을 권장합니다.
- 포트를 인터넷에 직접 여는 방식은 보안상 권장하지 않습니다.

---

## 폴더 구조
```
chem-relay/
├─ docker-compose.yml      # Container Manager 프로젝트 정의
├─ Dockerfile              # python:3.12-slim (amd64)
├─ requirements.txt
├─ cache/                  # 조회 캐시 (자동 생성, 재시작해도 유지)
└─ app/
   ├─ main.py              # FastAPI: /api/chem + UI 서빙
   └─ static/
      └─ index.html        # 웹 콘솔
```

---

## 지금 자동으로 되는 것 / 확장 지점

| 항목 | 상태 | 비고 |
|---|---|---|
| 기본정보·이명·구조식·화학식 | ✅ 자동 | 우선순위 1) CAS Common Chemistry 2) PubChem (아래 참고) |
| 기존/신규화학물질 판단 | ✅ 자동(CAS 입력 시만) | CAS No. 입력일 때만 판단. 물질명 입력은 혼합물 오판 방지를 위해 자동판단하지 않고 확인 링크만 제공 |
| GHS 분류(H코드·픽토그램·출처) | ✅ 자동 | 국내 유해성심사 고시 + 물질별 제출 자료('참고 분류표시' 시트) |
| 유해성 요약 | ✅ 자동 | 국내 유해성심사 + EU CLH(조화분류)만 근거로 사용 |
| 화학물질정보처리시스템(KREACH) 신고·등록 유해성분류·용도 | ✅ 자동(스크래핑) | 공개 API가 없어 검색→상세 폼 POST를 재현. 사이트 개편 시 `fetch_kreach()`만 수정 |
| 국내 규제/우려/개별 독성값 | 🔧 링크 | 아래 확장 지점 참고 |
| 조회 캐시 | ✅ | 30일, `cache/` 폴더 |

### 참고자료 우선순위 — CAS Common Chemistry API 연결

기본정보(물질명·이명·CAS·분자식·SMILES·InChIKey)는 **1) CAS Common Chemistry, 2) PubChem** 순서로 조회하고,
어느 쪽 자료인지 화면에 표시합니다. CAS Common Chemistry는 API 키가 있어야 조회되며,
키가 없으면 자동으로 2순위(PubChem)만 사용합니다(기본값, 추가 설정 불필요).

**설정 방법(선택)**
1. https://www.cas.org/services/commonchemistry-api 에서 API 키 신청
2. Container Manager → 프로젝트 → 환경변수, 또는 `docker-compose.yml`에 `CAS_CC_API_KEY=` 뒤에 키를 넣습니다.
3. 프로젝트를 재시작합니다.

### 2·4번 자동 생성 (하이브리드) — Claude API 연결

콘솔의 **2번 항목**에 있는 **"자세히 생성"** 버튼을 누르면, 서버가 Claude를 호출해
**국내외 규제현황(2번)** 과 **국내 우려·용도·노출경로(4번)** 를 출처와 함께 정리합니다.
버튼을 눌렀을 때만 호출되므로 비용이 통제되고, 결과는 캐시에 저장돼 재조회는 무료입니다.

**설정 방법**
1. https://console.anthropic.com 에서 API 키 발급 (`sk-ant-...`)
2. Container Manager → 프로젝트 → 환경변수, 또는 `docker-compose.yml`의
   `ANTHROPIC_API_KEY=` 뒤에 키를 넣습니다.
   (키를 파일에 직접 쓰기 싫으면 프로젝트 폴더에 `.env` 파일을 만들고
   `ANTHROPIC_API_KEY=sk-ant-...` 한 줄만 적으면 compose가 자동으로 읽습니다.)
3. 프로젝트를 **재시작**합니다.
4. 키가 없으면 "자세히 생성"만 비활성이고, 나머지 기능은 그대로 무료로 동작합니다.

**모델·비용**
- 기본 모델: `claude-sonnet-5` (규제 정리에 적합한 가성비 모델).
  모델 문자열은 바뀔 수 있으니 https://platform.claude.com/docs/en/about-claude/models 에서
  현재 값을 확인하세요. `ANTHROPIC_MODEL` 환경변수로 교체 가능합니다.
- 물질 1건당 소액(입력·출력 토큰 기준). 캐시되므로 같은 물질 재조회는 0원.

**중요 — 검수 전제**
- Claude가 만든 규제·우려 내용은 **초안**입니다. 화면에 신뢰도(high/medium/low)와
  검수 주의사항이 함께 표시되며, 각 항목에 출처를 답니다.
- 규제 정보는 틀리면 문제가 되므로, **게시·활용 전 출처 확인과 사람 검수**를 반드시 하세요.
- API 키는 서버(NAS) 안에만 있고 브라우저에는 절대 노출되지 않습니다.

### 국내 DB 스크래핑 — 화학물질정보처리시스템(KREACH)
`app/main.py`의 `fetch_kreach(cas, name)` 함수가 KREACH 정보공개 화면(검색→상세)을
대신 조회해 신고ㆍ등록 정보의 유해성 분류·표시, 용도정보를 가져옵니다(`beautifulsoup4` 사용).
공개 API가 없어 실제 사이트의 검색 폼 POST를 재현하는 방식이라, 정부 사이트 개편 시
`fetch_kreach()`와 `_kreach_parse_tab()`만 고치면 되도록 분리해 뒀습니다.

### 국외 DB 대조 자료('참고 분류표시' 시트) 갱신
물질별 결과물 엑셀(xlsm)의 2번째 시트 '(참고) 분류표시'(국외 DB별 유해성 분류·표시)를
`app/data/classification_reference.json`(CAS 기준)으로 미리 추출해 3번 항목에 표시합니다.
결과물 엑셀이 갱신되면 아래 스크립트를 다시 실행해 JSON을 갱신하세요.
```
py scripts/build_classification_reference.py
```

---

## 문제 해결
- **접속이 안 됨** → Container Manager에서 컨테이너가 `running`인지 확인,
  방화벽(제어판 → 보안 → 방화벽)에서 8000 포트 허용.
- **조회가 느림/실패** → NAS가 인터넷에 나갈 수 있는지 확인(PubChem 접속 필요).
  첫 조회 후에는 캐시로 빨라집니다.
- **UVCB(석유유분 등)** → PubChem에 구조 레코드가 없어 "레코드 없음"으로 표시됩니다.
  이는 정상이며, 해당 물질은 DB 링크로 확인하는 것이 맞습니다.
