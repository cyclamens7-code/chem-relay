"""
US NTP Report on Carcinogens(RoC, 15th edition) CASRN 목록을
app/data/ntp_roc.json 으로 변환한다.

NTP RoC는 의회 보고서로, 신규 발간 시(수년 주기)에만 바뀌므로
매 요청마다 스크래핑하지 않고 미리 받아둔 표를 조회한다.

원본: https://ntp.niehs.nih.gov/sites/default/files/ntp/roc/content/roc15_casrn_index.xlsx
사용:
  py scripts/build_ntp_roc.py
"""
import json
import pathlib
import re
import sys

import httpx
import openpyxl

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "app" / "data" / "ntp_roc.json"
SRC_URL = "https://ntp.niehs.nih.gov/sites/default/files/ntp/roc/content/roc15_casrn_index.xlsx"

CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")
LISTING_KO = {
    "Known": "발암성 확인됨(Known to be a Human Carcinogen)",
    "RAHC": "발암성으로 합리적으로 예상됨(Reasonably Anticipated to be a Human Carcinogen)",
}


def main():
    r = httpx.get(SRC_URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    tmp = ROOT / "scripts" / "_ntp_roc15_casrn_index.xlsx"
    tmp.write_bytes(r.content)

    wb = openpyxl.load_workbook(tmp, data_only=True)
    ws = wb["CAS #s"]
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        cas, name, listing = (row + (None, None, None))[:3]
        if not cas:
            continue
        cas = str(cas).strip()
        if not CAS_RE.match(cas):
            continue
        listing = (listing or "").strip()
        out[cas] = {
            "name": (name or "").strip(),
            "listing": listing,
            "listing_ko": LISTING_KO.get(listing, listing),
        }
    tmp.unlink(missing_ok=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료: {len(out)}개 CAS 저장 -> {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
