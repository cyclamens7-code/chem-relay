"""총칭명 검토 — 별표 1 / Lexicon guide 기준의 형식 자동점검 (AI 없이 문자열만으로 판단 가능한 항목)."""
import json
import re
from pathlib import Path

KNOWLEDGE = Path(__file__).parent / "data" / "generic"
SUBSTITUENTS = json.loads((KNOWLEDGE / "substituents.json").read_text(encoding="utf-8"))
LEXICON = json.loads((KNOWLEDGE / "lexicon_families.json").read_text(encoding="utf-8"))

FAIL, WARN, INFO = "부적합", "확인필요", "참고"

CHAIN_TERMS = ["alkynyl", "alkenyl", "alkyne", "alkene", "alkane", "alkyl"]
# alkanoate, alkanol, alkoxy 처럼 사슬 총칭어가 다른 어미와 붙은 형태까지 포함
CHAIN_RE = re.compile(r"alk(?:ynyl|enyl|yl|adien|yn|en|an|oxy)")
RING_TERMS = ["carbomonocycl", "carbopolycycl", "heteromonocycl", "heteropolycycl"]
HALOGENS = {
    "fluorine": ["fluor", "fluro"],
    "chlorine": ["chlor"],
    "bromine": ["brom"],
    "iodine": ["iod"],
}
HALOGEN_GENERIC = ["halo", "halide"]
METAL_GENERIC = ["metal", "lanthan", "actin", "rare earth"]
# 원소명 어간 -> (표시명, 분류)
METALS = {
    "lithium": ("Li", "alkali metal"), "sodium": ("Na", "alkali metal"), "potassium": ("K", "alkali metal"),
    "rubidium": ("Rb", "alkali metal"), "caesium": ("Cs", "alkali metal"), "cesium": ("Cs", "alkali metal"),
    "beryllium": ("Be", "alkaline earth metal"), "magnesium": ("Mg", "alkaline earth metal"),
    "calcium": ("Ca", "alkaline earth metal"), "strontium": ("Sr", "alkaline earth metal"),
    "barium": ("Ba", "alkaline earth metal"),
    "titanium": ("Ti", "transition metal"), "vanadium": ("V", "transition metal"),
    "chromium": ("Cr", "transition metal"), "manganese": ("Mn", "transition metal"),
    "iron": ("Fe", "transition metal"), "ferr": ("Fe", "transition metal"),
    "cobalt": ("Co", "transition metal"), "nickel": ("Ni", "transition metal"),
    "copper": ("Cu", "transition metal"), "cupr": ("Cu", "transition metal"),
    "zinc": ("Zn", "transition metal"), "zirconium": ("Zr", "transition metal"),
    "molybdenum": ("Mo", "transition metal"), "ruthenium": ("Ru", "transition metal"),
    "rhodium": ("Rh", "transition metal"), "palladium": ("Pd", "transition metal"),
    "silver": ("Ag", "transition metal"), "cadmium": ("Cd", "transition metal"),
    "tungsten": ("W", "transition metal"), "platinum": ("Pt", "transition metal"),
    "gold": ("Au", "transition metal"), "mercury": ("Hg", "transition metal"),
    "hafnium": ("Hf", "transition metal"), "yttrium": ("Y", "transition metal"),
    "aluminium": ("Al", "metal"), "aluminum": ("Al", "metal"), "gallium": ("Ga", "metal"),
    "indium": ("In", "metal"), "tin": ("Sn", "metal"), "stann": ("Sn", "metal"),
    "lead": ("Pb", "metal"), "plumb": ("Pb", "metal"), "bismuth": ("Bi", "metal"),
    "thallium": ("Tl", "metal"), "antimony": ("Sb", "metal"),
    "cerium": ("Ce", "metal"), "lanthanum": ("La", "metal"), "neodymium": ("Nd", "metal"),
}
ALKALI_WRONG = {
    "alkali metal": "alkali metal", "alkaline earth metal": "alkaline earth metal",
    "transition metal": "transition metal",
}
VAGUE_TERMS = ["proprietary", "confidential", "trade secret", "secret", "영업비밀", "비공개", "기밀", "unknown", "미상"]
TRADE_MARKS = ["®", "™", "(r)", "(tm)"]
STEREO = re.compile(r"\((?:[0-9]*[RSEZ],?)+\)|(?<![a-z])(?:cis|trans|rac|rel|meso)-|(?<![A-Za-z])[DL]-", re.I)
MULTIPLIERS = {"mono", "di", "tri", "tetra", "penta", "hexa", "hepta", "octa", "nona", "deca",
               "bis", "tris", "tetrakis", "pentakis", "poly"}
GENERIC_VOCAB = set(CHAIN_TERMS) | {"substituted", "halogenated", "halo", "metal", "transition", "alkali",
                                     "alkaline", "earth", "salt", "salts", "polymer", "with", "and", "of",
                                     "acid", "ester", "esters", "derivative", "derivatives", "compound",
                                     "compounds", "organic", "inorganic", "reaction", "product", "products",
                                     "resin", "cation", "anion", "the", "c", "branched", "linear"}


def _f(level, title, detail, basis, location=""):
    return {"level": level, "title": title, "detail": detail, "basis": basis, "location": location}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _count(term: str, text: str) -> int:
    return len(re.findall(re.escape(term), text))


def _find_metals(low: str):
    found = {}
    for stem, (sym, cls) in METALS.items():
        # 'tin', 'lead', 'iron' 은 다른 단어의 일부일 수 있으므로 단어 경계로만 인식
        pat = rf"(?<![a-z]){stem}(?![a-z])" if stem in ("tin", "lead", "iron", "gold", "silver") else stem
        if re.search(pat, low):
            found[sym] = (stem, cls)
    return found


def _find_halogens(low: str):
    # 'iod' 가 'period', 'chlor' 가 다른 맥락에 섞이는 경우를 줄이기 위해 흔한 오탐 제거
    cleaned = low.replace("period", "")
    return [el for el, stems in HALOGENS.items() if any(s in cleaned for s in stems)]


def lexicon_matches(generic: str):
    """총칭명에 포함된 Lexicon family / sub-family 명칭(단·복수 무시)을 찾는다."""
    low = generic.lower()
    hits = []
    for fam in LEXICON:
        for label, kind in [(fam["family"], "family")] + [(s, "sub-family") for s in fam["sub"]]:
            key = re.sub(r"\s*\(.*?\)\s*", " ", label).strip().lower()
            variants = {key, key.rstrip("s"), key.replace(" and derivatives", " derivative"),
                        key.replace("s and derivatives", " derivative"),
                        re.sub(r"s\b", "", key)}
            if any(len(v) > 3 and v in low for v in variants):
                hits.append({"no": fam["no"], "kind": kind, "name": label})
    # 더 긴(구체적인) 명칭이 있으면 그 안에 포함되는 짧은 명칭은 제외
    names = [h["name"].lower() for h in hits]
    return [h for h in hits if not any(h["name"].lower() != n and h["name"].lower().rstrip("s") in n for n in names)]


def substituents_in(name: str):
    """원 명칭에 들어 있는 별첨 치환기(긴 것 우선)."""
    low = name.lower()
    found = []
    mult = "|".join(sorted(MULTIPLIERS, key=len, reverse=True))
    for s in sorted(SUBSTITUENTS, key=len, reverse=True):
        key = s.lower()
        if len(key) < 3 or any(key in f.lower() for f in found):
            continue
        # 앞은 단어 시작 또는 개수 접두사여야 하고, azo/azi 는 azole·azine·azide 의 일부가 아니어야 한다
        pat = rf"(?:(?<![a-z])|(?<![a-z])(?:{mult})){re.escape(key)}"
        if key in ("azo", "azi"):
            pat += r"(?![lndx])"
        if re.search(pat, low):
            found.append(s)
    return found


def check_name(name: str):
    out = []
    n = _norm(name)
    low = n.lower()
    if not n:
        return [_f(FAIL, "화학물질명 미입력", "화학물질명이 입력되지 않았습니다.", "검토 대상")]
    for a, b in ("()", "[]", "{}"):
        if n.count(a) != n.count(b):
            out.append(_f(FAIL, "괄호 불일치", f"'{a}' {n.count(a)}개, '{b}' {n.count(b)}개로 짝이 맞지 않습니다.",
                          "IUPAC/CAS 명명법(표기)", n))
    if any(t in low for t in TRADE_MARKS):
        out.append(_f(FAIL, "상품명 표지 포함", "®/™ 등 상품명 표지가 있습니다. 물질 식별이 곤란한 상품명은 신청 명칭으로 쓸 수 없습니다.",
                      "안내서 S-Ⅱ-1. S1"))
    if re.search(r"[가-힣]", n):
        out.append(_f(WARN, "한글 포함 명칭", "명칭에 한글이 포함되어 있습니다. 영문 IUPAC/CAS 명칭과 대응되는지 확인이 필요합니다.",
                      "안내서 S-Ⅱ-1. S1"))
    if not re.search(r"[A-Za-z]{4,}", n):
        out.append(_f(WARN, "체계명으로 보기 어려움", "영문 체계명 요소가 거의 없습니다(코드·약어 가능성).", "안내서 S-Ⅱ-1. S1"))
    if re.search(r",\s*[^,]*-\s*$", n) or re.search(r"-\s*,", n):
        out.append(_f(INFO, "CAS 색인명(역순 표기) 형식", "'모체-접미사, 접두사-' 형태의 CAS 색인명으로 보입니다.", "안내서 S-Ⅱ-1. S2"))
    if re.search(r"polymer with|reaction products?|homopolymer|copolymer|oligomer", low):
        out.append(_f(WARN, "고분자화합물/반응생성물 형식의 명칭",
                      "단량체·반응물 나열형 명칭입니다. 총칭명은 각 단량체·반응물에 별표1 제1~3호를 적용해야 하며, "
                      "미반응 단량체·반응물이 0.1 wt% 이상이면서 유해화학물질이면 해당 성분은 총칭명으로 바꿀 수 없습니다(잔류량 자료 확인). "
                      "Lexicon guide 방식은 결과물 구조·명칭을 함께 제출한 경우에만 적용할 수 있습니다.",
                      "별표1 제4호 / 안내서 S-Ⅱ-1. S3 ④"))
    if re.search(r"\bextracts?\b|\boils?\b|distillates?|petroleum|\bwax(es)?\b|\btar\b", low):
        out.append(_f(WARN, "추출물·UVCB 형식의 명칭",
                      "원료·공정으로 표기된 명칭은 분자구조에 따른 명칭이 아니어서 구조 기반 검토가 제한됩니다.",
                      "안내서 S-Ⅱ-2. 9~12"))
    if STEREO.search(n):
        out.append(_f(INFO, "입체/이성질체 표지 포함", "입체·이성질체 표지는 총칭명 판단에서 제외하고 나머지 구조로 검토합니다.",
                      "안내서 S-Ⅱ-2. 13"))
    if re.search(r"[A-Za-z]\s{2,}[A-Za-z]", name or ""):
        out.append(_f(INFO, "공백 표기", "명칭 중간에 연속 공백이 있습니다.", "표기"))
    return out


def check_generic(name: str, generic: str):
    out = []
    n, g = _norm(name), _norm(generic)
    nl, gl = n.lower(), g.lower()
    if not g:
        return [_f(FAIL, "총칭명 미입력", "총칭명이 입력되지 않았습니다.", "검토 대상")], {"method": "판단불가"}

    lex = lexicon_matches(g)
    masked_terms = [t for t in RING_TERMS + ["substituted", "halo", "metal"] if t in gl] + CHAIN_RE.findall(gl)
    if lex and not masked_terms:
        method = "Lexicon guide (별표1 제8호)"
    elif masked_terms:
        method = "별표1 제1~7호 (부분 마스킹)" + (" + Lexicon 명칭 혼용" if lex else "")
    else:
        method = "판단불가"
    meta = {"method": method, "lexicon_matches": lex, "annex_substituents_in_name": substituents_in(n)}

    if re.sub(r"[\s\-]", "", gl) == re.sub(r"[\s\-]", "", nl):
        out.append(_f(WARN, "총칭명이 원 명칭과 동일", "총칭명으로 바뀐 부분이 없습니다.", "별표1"))
    for v in VAGUE_TERMS:
        if v in gl:
            out.append(_f(FAIL, "구조 정보가 없는 표현", f"'{v}' 는 별표1 및 Lexicon guide 어디에도 없는 표현입니다.", "별표1 전체", v))
    if any(h["no"] == "650" for h in lex):
        out.append(_f(FAIL, "사용 금지 family", "Lexicon family 650 'Various substances' 는 사용할 수 없습니다.", "Lexicon guide 650 주석", "Various substances"))

    # 제3호: substituted
    sub_n = _count("substituted", gl)
    if sub_n >= 2:
        out.append(_f(WARN, "'substituted' 반복 사용",
                      f"'substituted' 가 {sub_n}회 사용되었습니다. 같은 치환기의 반복일 때만 허용되며, 서로 다른 치환기에 중복 사용하면 부적합입니다.",
                      "별표1 제3호 (2)", "substituted"))
    if sub_n and not meta["annex_substituents_in_name"]:
        out.append(_f(WARN, "'substituted' 로 바꿀 별첨 치환기 미확인",
                      "원 명칭에서 별첨 목록의 치환기를 찾지 못했습니다. 'substituted' 는 별첨 치환기에만 사용할 수 있습니다.",
                      "별표1 제3호", "substituted"))

    # 제1호 단서 / 제6호: 사슬계 총칭어 중복
    chain_hits = CHAIN_RE.findall(gl)
    ranges = re.findall(r"c\s*\d+\s*(?:[-~–]\s*(?:c\s*)?\d+)?", gl)
    if len(chain_hits) >= 2 and len(set(ranges)) < len(chain_hits):
        out.append(_f(FAIL, "여러 사슬계의 탄소수 범위 미구분",
                      f"사슬계 총칭어가 {len(chain_hits)}회({', '.join(chain_hits)}) 사용되었으나 서로 다른 탄소수 범위 표기는 {len(set(ranges))}개입니다. "
                      "여러 사슬계는 탄소원자 개수의 범위를 달리하여 명명해야 합니다. (같은 사슬이 반복되는 구조라면 예외)",
                      "별표1 제1호 단서, 제6호", ", ".join(chain_hits)))
    for t in RING_TERMS:
        if _count(t, gl) >= 2:
            out.append(_f(WARN, "동일 고리계 총칭어 반복",
                          f"'{t}~' 가 2회 이상 사용되었습니다. 서로 다른 고리계에 동일한 총칭명을 쓰면 부적합입니다(같은 고리의 반복이면 허용).",
                          "별표1 제6호", t))

    # 제7호: 할로겐 / 금속
    for el in _find_halogens(nl):
        stems = HALOGENS[el]
        if not any(s in gl for s in stems) and not any(h in gl for h in HALOGEN_GENERIC):
            # 할로겐 치환기(chloro 등)는 별첨 치환기이므로 'substituted' 로 대체되었을 수 있다
            out.append(_f(WARN if sub_n else FAIL, f"할로겐({el}) 정보 누락",
                          f"원 명칭에는 {el} 가 있으나 총칭명에는 해당 원소명도 'halogenated/halo' 총칭어도 없습니다."
                          + (" 'substituted' 로 대체된 것인지, 그렇다면 다른 치환기에 중복 사용되지 않았는지 확인하세요." if sub_n else ""),
                          "별표1 제7호 / 안내서 S-Ⅱ-1. S3(할로겐은 중요원소)", el))
    if any(h in gl for h in ["halogenat", "halo"]) and not _find_halogens(nl) and "halide" not in nl:
        out.append(_f(FAIL, "원 명칭에 없는 할로겐 표현", "총칭명에 halo/halogenated 가 있으나 원 명칭에서 할로겐을 찾지 못했습니다.",
                      "별표1 제7호", "halo"))
    name_metals = _find_metals(nl)
    gen_metals = _find_metals(gl)
    for sym, (stem, cls) in name_metals.items():
        if sym in gen_metals:
            continue
        if not any(m in gl for m in METAL_GENERIC):
            out.append(_f(FAIL, f"금속원소({sym}) 정보 누락",
                          f"원 명칭에는 금속원소 {sym} 가 있으나 총칭명에는 원소명도 'metal' 계열 총칭어도 없습니다.",
                          "별표1 제7호 / 안내서 S-Ⅱ-2. 7(금속은 중요원소)", stem))
        else:
            for wrong in ALKALI_WRONG:
                if wrong in gl and cls != wrong and not (wrong == "alkali metal" and "alkaline" in gl and cls == "alkaline earth metal"):
                    out.append(_f(FAIL, f"금속 분류 불일치({sym})",
                                  f"{sym} 는 {cls} 인데 총칭명에는 '{wrong}' 로 표기되어 있습니다.", "별표1 제7호", wrong))
                    break
    if any(m in gl for m in ["metal"]) and not name_metals and "metal" not in nl:
        out.append(_f(WARN, "원 명칭에서 금속원소 미확인", "총칭명에 metal 표현이 있으나 원 명칭에서 금속원소를 자동으로 찾지 못했습니다.",
                      "별표1 제7호", "metal"))

    # 부분 마스킹 방식: 총칭명에 남은 부분이 원 명칭에 실제로 있는지
    if (CHAIN_RE.search(gl) or any(t in gl for t in RING_TERMS + ["substituted"])) and not lex:
        tokens = re.findall(r"[a-z]{4,}", gl)
        strange = []
        for t in tokens:
            if t in GENERIC_VOCAB or t in MULTIPLIERS or any(t.startswith(r) for r in RING_TERMS):
                continue
            base = t
            for m in sorted(MULTIPLIERS, key=len, reverse=True):
                if base.startswith(m) and len(base) - len(m) >= 4:
                    base = base[len(m):]
                    break
            base = CHAIN_RE.sub("", base)
            if len(base) >= 4 and base not in nl and t not in nl:
                strange.append(t)
        if strange:
            out.append(_f(WARN, "원 명칭에 없는 표현",
                          "별표1 제1~7호 방식에서는 총칭어로 바꾼 부분 외에는 원 명칭을 그대로 유지해야 합니다. "
                          f"다음 표현은 원 명칭에서 찾을 수 없습니다: {', '.join(dict.fromkeys(strange))}",
                          "별표1 제1~7호", ", ".join(dict.fromkeys(strange))))
    if method == "판단불가":
        out.append(_f(WARN, "총칭명 작성 방식 미확인",
                      "별표1의 총칭어(alkyl, carbomonocyclic, substituted 등)도 Lexicon guide 의 family/sub-family 명칭도 자동으로 찾지 못했습니다.",
                      "별표1 제1~8호"))
    return out, meta


def run(name: str, generic: str):
    name_findings = check_name(name)
    generic_findings, meta = check_generic(name, generic)
    return {"name": name_findings, "generic": generic_findings, "meta": meta}
