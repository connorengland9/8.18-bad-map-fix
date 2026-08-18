"""
CE-PIREP upstream diagnostic.

Run this from your machine (NOT Vercel) to see exactly what each upstream
endpoint returns:

    pip install requests
    python diagnose.py

Then run it again from a Vercel deployment if local looks healthy -- a
difference between the two points at IP-based filtering rather than a bug.
"""

import json
import sys
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AWC_BASE = "https://aviationweather.gov/api/data"
IDS = "PGUM,PGUA,PGSN,PGRO,PGWT"

GOOD_UA = {
    "User-Agent": "CE-PIREP-diagnostic/1.0 (contact: connorengland9@gmail.com)",
    "Accept": "application/json",
}


def probe(label, url, params=None, headers=None, verify=True):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    try:
        res = requests.get(url, params=params, headers=headers,
                           timeout=15, verify=verify)
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        return

    print(f"  final URL : {res.url}")
    print(f"  status    : {res.status_code}")
    print(f"  UA sent   : {res.request.headers.get('User-Agent')}")
    print(f"  length    : {len(res.content)} bytes")

    if res.status_code == 204:
        print("  -> 204 No Content: request was valid, no data matched.")
        return
    if res.status_code != 200:
        print(f"  body      : {res.text[:400]}")
        return

    try:
        data = res.json()
    except ValueError:
        print(f"  NOT JSON  : {res.text[:400]}")
        return

    if isinstance(data, dict):
        print(f"  dict keys : {list(data.keys())}")
        data = data.get("data", [])

    if isinstance(data, list):
        print(f"  records   : {len(data)}")
        if data:
            print(f"  fields    : {sorted(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
            print("  first     :")
            print("    " + json.dumps(data[0], indent=2)[:900].replace("\n", "\n    "))


def main():
    # 1. What the ORIGINAL code did -- expect this to fail.
    probe(
        "1. ORIGINAL METAR call (www host, default UA, `_` cache-buster)",
        "https://www.aviationweather.gov/api/data/metar",
        params={"ids": IDS, "format": "json", "taf": "false",
                "hours": 2, "_": 1234567890},
    )

    # 2. Same call, cache-buster removed.
    probe(
        "2. METAR without the `_` param (default UA)",
        f"{AWC_BASE}/metar",
        params={"ids": IDS, "format": "json", "taf": "false", "hours": 3},
    )

    # 3. The fixed call.
    probe(
        "3. METAR fixed (apex host, custom UA, no `_`)",
        f"{AWC_BASE}/metar",
        params={"ids": IDS, "format": "json", "taf": "false", "hours": 3},
        headers=GOOD_UA,
    )

    # 4. The PIREP endpoint the original code used -- does not exist.
    probe(
        "4. ORIGINAL PIREP call (/aircraftreport + bbox)",
        "https://www.aviationweather.gov/api/data/aircraftreport",
        params={"format": "json", "bbox": "8.0,139.0,19.0,150.0",
                "age": 2, "_": 1234567890},
    )

    # 5. The correct PIREP endpoint.
    probe(
        "5. PIREP fixed (/pirep + id/distance)",
        f"{AWC_BASE}/pirep",
        params={"id": "PGUM", "distance": 300, "age": 2, "format": "json"},
        headers=GOOD_UA,
    )

    # 6. NavCanada fallback -- likely empty for Guam, that's expected.
    nc_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0 Safari/537.36",
        "Referer": "https://plan.navcanada.ca/wxrecall/",
    }
    probe(
        "6. NavCanada METAR fallback",
        f"https://plan.navcanada.ca/weather/api/alpha/?site={IDS}&alpha=metar",
        headers=nc_headers,
        verify=False,
    )

    probe(
        "7. NavCanada PIREP fallback",
        "https://plan.navcanada.ca/weather/api/alpha/?site=PGUM&radius=300&alpha=pirep",
        headers=nc_headers,
        verify=False,
    )

    print(f"\n{'=' * 70}")
    print("Compare 1 vs 3 and 4 vs 5. If 3 and 5 return records and 1 and 4")
    print("do not, the endpoint/parameter changes are the whole problem.")
    print("If 3 fails HERE too, check the status page:")
    print("  https://aviationweather.gov/tools/status/")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
