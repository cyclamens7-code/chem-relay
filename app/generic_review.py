"""총칭명 검토 — AI 프롬프트·출력 스키마와 결과 정리.

Gemini 호출 자체는 main.py 의 공통 헬퍼(_gemini_call)를 쓰고, 여기서는
무엇을 물을지(프롬프트)와 돌아온 JSON 을 화면이 쓰기 좋게 다듬는 일만 한다.
"""
import base64
import json

from . import generic_rules as rules

KNOWLEDGE = rules.KNOWLEDGE

VERDICT = ["적합", "부적합", "판단곤란"]
SEVERITY = ["부적합", "확인필요", "경미"]


def _issue_schema():
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": SEVERITY},
                "location": {"type": "string", "description": "문제가 되는 명칭의 부분 문자열(그대로 인용). 전체에 해당하면 빈 문자열"},
                "problem": {"type": "string", "description": "무엇이 왜 잘못되었는지 구조식 근거와 함께 구체적으로"},
                "basis": {"type": "string", "description": "근거 기준 (예: '별표1 제3호 (2)', 'Lexicon guide 607', 'IUPAC P-31.1.4 최소 위치번호')"},
                "suggestion": {"type": "string", "description": "수정 방향 또는 수정안"},
            },
            "required": ["severity", "location", "problem", "basis", "suggestion"],
            "additionalProperties": False,
        },
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "structure": {
            "type": "object",
            "properties": {
                "readable": {"type": "boolean", "description": "첨부자료에서 구조식을 판독할 수 있었는지"},
                "source_note": {"type": "string", "description": "어느 파일/쪽의 어떤 그림을 판독했는지, 판독이 불확실한 부분"},
                "confidence": {"type": "string", "enum": ["높음", "보통", "낮음"]},
                "substance_type": {"type": "string", "enum": ["단일 유기화합물", "염", "금속착물/유기금속", "무기화합물", "고분자", "반응생성물/UVCB", "혼합물", "판독불가"]},
                "description": {"type": "string", "description": "판독한 구조를 말로 설명 (모체, 치환기와 위치, 결합)"},
                "smiles": {"type": "string", "description": "판독한 구조의 SMILES. 확신할 수 없으면 빈 문자열"},
                "molecular_formula": {"type": "string"},
                "name_from_structure": {"type": "string", "description": "판독한 구조에 대해 직접 붙인 IUPAC 명칭"},
                "chains": {"type": "array", "items": {"type": "string"}, "description": "사슬계 목록 (예: 'C12 포화 직쇄 알킬 (N에 결합)')"},
                "rings": {"type": "array", "items": {"type": "string"}, "description": "고리계 목록 (예: '벤젠 고리 1개 = carbomonocycle')"},
                "functional_groups": {"type": "array", "items": {"type": "string"}},
                "special_elements": {"type": "array", "items": {"type": "string"}, "description": "금속, 할로겐, C/H/N/O 외 원소"},
                "hazard_relevant_groups": {"type": "array", "items": {"type": "string"}, "description": "자기반응성/폭발성 관련 작용기, Lexicon 특정 sub-family 해당 구조"},
            },
            "required": ["readable", "source_note", "confidence", "substance_type", "description", "smiles",
                         "molecular_formula", "name_from_structure", "chains", "rings", "functional_groups",
                         "special_elements", "hazard_relevant_groups"],
            "additionalProperties": False,
        },
        "name_review": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": VERDICT},
                "nomenclature_type": {"type": "string", "enum": ["IUPAC 명칭", "CAS 색인명", "IUPAC/CAS 혼용 체계명", "관용명", "상품명/코드", "단량체·반응물 나열형", "원료·공정형(UVCB)", "기타"]},
                "structure_match": {"type": "string", "enum": ["일치", "부분 불일치", "불일치", "확인불가"]},
                "parsed_name": {"type": "string", "description": "제출 명칭을 접두사(치환기)-모체-접미사(class)로 분해한 설명"},
                "summary": {"type": "string"},
                "issues": _issue_schema(),
                "suggested_name": {"type": "string", "description": "수정이 필요하면 권장 명칭, 아니면 빈 문자열"},
            },
            "required": ["verdict", "nomenclature_type", "structure_match", "parsed_name", "summary", "issues", "suggested_name"],
            "additionalProperties": False,
        },
        "generic_review": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": VERDICT},
                "method": {"type": "string", "enum": ["별표1 제1~7호 (부분 마스킹)", "Lexicon guide (별표1 제8호)", "혼용", "판단불가"]},
                "mapping": {
                    "type": "array",
                    "description": "원 명칭의 각 부분이 총칭명에서 어떻게 표기되었는지 순서대로. 그대로 유지된 부분도 포함",
                    "items": {
                        "type": "object",
                        "properties": {
                            "original_part": {"type": "string"},
                            "generic_part": {"type": "string", "description": "총칭명에서 대응하는 부분. 누락되었으면 '(누락)'"},
                            "kind": {"type": "string", "enum": ["사슬계", "고리계", "별첨 치환기", "별첨 외 치환기", "위치/개수", "염 이온", "금속원소", "할로겐원소", "주 작용기(class)", "단량체/반응물", "유지", "Lexicon family", "기타"]},
                            "rule": {"type": "string", "description": "적용된 기준 (예: '별표1 제2호 (1)①')"},
                            "ok": {"type": "boolean"},
                            "comment": {"type": "string"},
                        },
                        "required": ["original_part", "generic_part", "kind", "rule", "ok", "comment"],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string"},
                "issues": _issue_schema(),
                "suggested_generic_names": {"type": "array", "items": {"type": "string"}, "description": "기준에 맞는 총칭명 예시 1~3개"},
            },
            "required": ["verdict", "method", "mapping", "summary", "issues", "suggested_generic_names"],
            "additionalProperties": False,
        },
        "overall": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": VERDICT},
                "summary": {"type": "string", "description": "검토자용 2~4문장 요약"},
                "supplement_request": {"type": "string", "description": "부적합/판단곤란일 때 신청인에게 보낼 보완요청 문안 초안. 적합이면 빈 문자열"},
            },
            "required": ["verdict", "summary", "supplement_request"],
            "additionalProperties": False,
        },
    },
    "required": ["structure", "name_review", "generic_review", "overall"],
    "additionalProperties": False,
}

ROLE = """당신은 「화학물질의 등록 및 평가 등에 관한 법률」에 따른 자료보호 신청 심사를 지원하는 화학물질 명명법 전문 검토자입니다.
신청 기업이 제출한 화학물질명과 희망 총칭명, 그리고 (제출된 경우) 구조식 자료를 받아 다음을 검토합니다. 구조식 자료는 제출되지 않을 수도 있습니다.

1단계 — 구조 판독: 첨부자료에서 구조식을 직접 읽어 구조를 확정합니다. 제출 명칭에 맞춰 구조를 해석하지 말고, 그림에 그려진 원자·결합·치환 위치를 독립적으로 읽으세요. 그림이 여러 개면 신청 물질에 해당하는 것을 고르고 어느 것을 읽었는지 밝히세요. 판독이 불확실한 부분은 추측으로 메우지 말고 불확실하다고 적고 confidence 를 낮추세요.
2단계 — 화학물질명 검토: 제출 명칭을 분해하여 그 명칭이 가리키는 구조를 도출하고, 1단계에서 읽은 구조와 원자 단위로 비교합니다. 그리고 명칭이 IUPAC 또는 CAS 명명법에 맞게 작성되었는지 검토합니다.
3단계 — 총칭명 검토: 총칭명이 어느 방식(별표1 제1~7호의 부분 마스킹 / 제8호의 Lexicon guide)으로 작성되었는지 판단하고, 그 방식의 기준에 따라 원 명칭의 각 부분과 총칭명의 대응을 하나씩 확인합니다. 총칭어가 실제 구조와 맞는지는 명칭이 아니라 1단계에서 읽은 구조를 기준으로 판단합니다.

판정 원칙:
- 판정은 아래 '검토 기준'에만 근거합니다. 기준에 없는 사유로 부적합 처리하지 않습니다.
- 지적 사항마다 명칭의 어느 부분이, 어떤 기준에, 왜 어긋나는지와 수정 방향을 적습니다. 심사자가 그대로 보완요청에 옮겨 쓸 수 있을 만큼 구체적이어야 합니다.
- severity: '부적합'은 기준 위반이 명확한 경우, '확인필요'는 제출자료만으로 판단할 수 없어 추가 자료가 필요한 경우, '경미'는 구조 특정에는 지장이 없는 표기 문제입니다.
- verdict: '부적합' 지적이 하나라도 있으면 부적합, '확인필요'만 있거나 제출된 구조식을 판독할 수 없으면 판단곤란, 그 외에는 적합입니다. overall.verdict 는 화학물질명과 총칭명 판정 중 더 나쁜 쪽을 따릅니다.
- 화학물질명이 부적합이어도 총칭명 검토는 생략하지 않습니다. 이때는 판독한 구조와 올바른 명칭을 기준으로 총칭명을 평가하고 그 사실을 밝힙니다.
- 구조식 자료가 제출되지 않은 경우: 정상적인 신청 형태이므로 그 자체로 감점하거나 판단곤란으로 처리하지 않습니다. 제출 명칭을 해석하여 그 명칭이 가리키는 구조를 도출하고, structure 항목(description, smiles, molecular_formula, chains, rings, functional_groups 등)을 그 도출 구조로 채웁니다. structure.readable=false, source_note 에는 '구조식 미제출 — 화학물질명으로부터 도출한 구조'라고 적고, name_from_structure 는 도출 구조에 대한 올바른 IUPAC 명칭을 씁니다. 화학물질명은 명명법 준수 여부와 구조를 하나로 특정할 수 있는지를 검토하고 structure_match 는 '확인불가'로 둡니다. 총칭명은 도출한 구조를 기준으로 검토합니다. 명칭만으로 구조를 특정할 수 없으면(관용명·상품명·모호한 명칭) 그 점을 지적합니다.
- 구조식 자료가 제출되었으나 판독할 수 없는 경우: structure.readable=false 로 하고 위와 같이 명칭 기준으로 검토하되, 판독 불가 사실을 '확인필요'로 지적합니다.
- '자동 형식점검 결과'와 'PubChem 조회 결과'가 주어지면 참고하되, 문자열 기반 점검이라 오탐이 있을 수 있으니 구조에 비추어 직접 확인한 뒤 채택 여부를 정하세요.
- 모든 설명은 한국어로 작성하고, 화학명·총칭어는 영문 그대로 씁니다."""


def system_text() -> str:
    rules_md = (KNOWLEDGE / "rules.md").read_text(encoding="utf-8")
    subs = ", ".join(rules.SUBSTITUENTS)
    lex = "\n".join(
        f"{f['no']} {f['family']}" + (": " + "; ".join(f["sub"]) if f["sub"] else "") for f in rules.LEXICON
    )
    return (
        f"{ROLE}\n\n{rules_md}\n\n"
        f"## D. 별표 1 <별첨> 치환기 목록 (제3호에서 'substituted' 로 바꿀 수 있는 치환기)\n{subs}\n\n"
        f"## E. Lexicon guide — families / sub-families 목록\n"
        f"((*) 표시는 해당 할로겐 family 에 따라 명시, (**) 는 quinone 포함, 650 은 사용 금지)\n{lex}\n\n"
        "## 출력 형식\n아래 JSON 스키마를 정확히 따르는 JSON 객체 하나만 출력하세요(설명·마크다운 금지).\n"
        + json.dumps(SCHEMA, ensure_ascii=False)
    )


def build_payload(files, name, generic, cas="", note="", local=None, pubchem=None) -> dict:
    """files: [(파일명, mime, bytes)] — 비어 있어도 된다(구조식 미제출)."""
    context = [f"화학물질명(신청 명칭): {name}", f"희망 총칭명: {generic}"]
    if cas:
        context.append(f"CAS No.: {cas}")
    if note:
        context.append(f"신청인/검토자 비고: {note}")
    if local:
        context.append("자동 형식점검 결과(참고):\n" + json.dumps(local, ensure_ascii=False, indent=1))
    if pubchem:
        context.append("PubChem 조회 결과(참고):\n" + json.dumps(pubchem, ensure_ascii=False, indent=1))
    if not files:
        context.append("※ 구조식 자료는 제출되지 않았습니다. 화학물질명으로부터 구조를 도출하여 검토하세요.")
    parts = []
    for fname, mime, raw in files:
        parts.append({"text": f"[첨부파일: {fname}]"})
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode("ascii")}})
    parts.append({"text": "\n\n".join(context)})
    return {
        "system_instruction": {"parts": [{"text": system_text()}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 32768,
            "responseMimeType": "application/json",
        },
    }


def _fill(value, schema):
    """스키마 강제가 없는 호출이라 빠진 항목이 있을 수 있다. 화면이 깨지지 않게 기본값으로 채운다."""
    t = schema.get("type")
    if t == "object":
        value = value if isinstance(value, dict) else {}
        return {k: _fill(value.get(k), s) for k, s in schema["properties"].items()}
    if t == "array":
        value = value if isinstance(value, list) else []
        return [_fill(v, schema["items"]) for v in value]
    if t == "boolean":
        return bool(value)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def normalize(parsed) -> dict:
    out = _fill(parsed, SCHEMA)
    for part in (out["name_review"], out["generic_review"], out["overall"]):
        if part["verdict"] not in VERDICT:
            part["verdict"] = "판단곤란"
    return out


def final_verdict(local, ai_result):
    levels = [f["level"] for f in local["name"] + local["generic"]]
    if ai_result:
        v = ai_result["overall"]["verdict"]
        if v == "적합" and rules.FAIL in levels:
            return "판단곤란", "AI 검토는 적합이나 자동 형식점검에서 부적합 항목이 있습니다. 해당 항목을 직접 확인하세요."
        return v, ""
    if rules.FAIL in levels:
        return "부적합", "자동 형식점검 기준 판정입니다 (AI 검토는 수행되지 않음)."
    if rules.WARN in levels:
        return "판단곤란", "자동 형식점검 기준 판정입니다 (AI 검토는 수행되지 않음)."
    return "판단곤란", "형식상 문제는 발견되지 않았으나 AI 검토가 수행되지 않아 적합 여부를 확정할 수 없습니다."
