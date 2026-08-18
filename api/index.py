import requests
import time
import json
import re
import urllib3
import concurrent.futures
from flask import Flask, render_template, jsonify, make_response
from datetime import datetime, timezone, timedelta

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- CONFIGURATION ---
MAIN_AIRPORTS = [
    {"id": "PGUM", "name": "Agana Airport"},
    {"id": "PGUA", "name": "Andersen AFB"},
    {"id": "PGSN", "name": "Saipan Airport"}
]

AUX_AIRPORTS = [
    {"id": "PGRO", "name": "Rota Int'l"},
    {"id": "PGWT", "name": "West Tinian"}
]

# AWC asks for a custom User-Agent -- the default python-requests UA gets
# caught by their automated filtering. Put a real contact address here so
# they can reach you instead of silently blocking.
AWC_BASE = "https://aviationweather.gov/api/data"
AWC_HEADERS = {
    "User-Agent": "CE-PIREP/2.0 (Guam ops dashboard; contact: connorengland9@gmail.com)",
    "Accept": "application/json",
}
AWC_TIMEOUT = 10


class Diag:
    """Collects upstream failures per request so they surface in /api/data
    instead of vanishing into a print() nobody reads."""

    def __init__(self):
        self.errors = []

    def add(self, source, msg):
        self.errors.append(f"{source}: {msg}")
        print(f"   [ERROR] {source}: {msg}")


# --- LOGIC HELPERS ---
def get_cloud_base(layer):
    base = layer.get('base')
    try:
        if base is not None:
            return int(base)
    except (ValueError, TypeError):
        pass
    return None


def metar_body(raw_text):
    """Everything before RMK -- avoids matching TS/GR inside remarks."""
    if not raw_text:
        return ""
    return re.split(r'\bRMK\b', raw_text.upper())[0]


# A weather-phenomena group is built entirely from these 2-letter codes,
# optionally prefixed with intensity (+/-) or VC, and one descriptor --
# e.g. "+TSGR" (heavy thunderstorm hail). Matching the whole token against
# this grammar (rather than scanning raw text for bare substrings) is what
# keeps a station ID like "PGRO" from being mistaken for hail ("GR").
_WX_DESCRIPTORS = "MI|PR|BC|DR|BL|SH|TS|FZ"
_WX_PHENOMENA = "DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS"
WX_TOKEN_RE = re.compile(rf'^(?:[-+]|VC)?(?:{_WX_DESCRIPTORS})?(?:{_WX_PHENOMENA})+$')


def extract_wx_tokens(raw_text):
    """Pull only genuine weather-phenomena groups out of a raw METAR body,
    ignoring the station ID / wind / cloud groups that can coincidentally
    contain the same 2-letter codes."""
    return " ".join(t for t in metar_body(raw_text).split() if WX_TOKEN_RE.match(t))


def check_pirep_condition(station_data):
    conditions = []

    # 1. CEILING
    ceiling_layers = []
    clouds = station_data.get('clouds') or []
    for layer in clouds:
        cover = layer.get('cover') or ''
        base = get_cloud_base(layer)
        if cover in ['BKN', 'OVC', 'VV'] and base is not None and base <= 5000:
            ceiling_layers.append(base)
    if ceiling_layers:
        conditions.append(f"CIG {min(ceiling_layers)}FT")

    # 2. VISIBILITY
    vis = station_data.get('visib')
    if vis is not None:
        try:
            if isinstance(vis, str) and '+' in vis:
                val = float(vis.replace('+', ''))
            else:
                val = float(vis)
            if val <= 5.0:
                v_str = vis if isinstance(vis, str) else str(val)
                conditions.append(f"VIS {v_str}SM")
        except (ValueError, TypeError):
            pass

    # 3. HAZARDOUS WX
    # AWC returns "wxString": null whenever there's no significant weather.
    # The old `.get('wxString', "")` returned None in that case and the next
    # line raised TypeError, which 500'd the entire /api/data route.
    wx = (station_data.get('wxString') or "").upper()
    if not wx:
        wx = extract_wx_tokens(station_data.get('rawOb') or "")

    if 'TS' in wx: conditions.append("THUNDERSTORM")
    if 'VA' in wx: conditions.append("VOLCANIC ASH")
    if 'FC' in wx: conditions.append("FUNNEL CLOUD")
    if 'GR' in wx: conditions.append("HAIL")
    if 'WS' in wx: conditions.append("WIND SHEAR")
    if '+RA' in wx: conditions.append("HEAVY RAIN")

    if conditions:
        return True, " / ".join(sorted(list(set(conditions))))
    return False, "PIREP NOT REQUIRED"


def check_ifr_status(station_data):
    clouds = station_data.get('clouds') or []
    for layer in clouds:
        cover = layer.get('cover') or ''
        base = get_cloud_base(layer)
        if cover in ['BKN', 'OVC', 'VV'] and base is not None and base < 1000:
            return True

    vis = station_data.get('visib')
    if vis is not None:
        try:
            if isinstance(vis, str) and '+' in vis:
                val = float(vis.replace('+', ''))
            else:
                val = float(vis)
            if val < 3.0:
                return True
        except (ValueError, TypeError):
            pass

    return False


def parse_ddhhmm_from_text(raw_text):
    """DDHHMMZ -> ISO8601. Walks back a month when needed and never raises on
    a day that doesn't exist in the current month (the old version did)."""
    if not raw_text:
        return None

    match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_text)
    if not match:
        return None

    day, hour, minute = map(int, match.groups())
    if not (1 <= day <= 31 and hour <= 23 and minute <= 59):
        return None

    now = datetime.now(timezone.utc)

    for months_back in (0, 1):
        year, month = now.year, now.month - months_back
        if month < 1:
            month += 12
            year -= 1
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            continue
        if candidate <= now + timedelta(hours=6):  # slop for clock skew
            return candidate.isoformat()

    return None


def to_iso(value):
    """AWC hands back epoch ints on some fields and ISO strings on others."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OSError):
            return ""
    value = str(value)
    if 'T' in value and not value.endswith('Z') and '+' not in value:
        value += 'Z'
    return value


# --- REDUNDANT METAR FETCHING LOGIC ---

def extract_visibility(raw_text):
    match = re.search(r'\b(M)?((\d+)\s+)?(\d+)/(\d+)SM\b', raw_text)
    if match:
        whole = float(match.group(3)) if match.group(3) else 0.0
        num = float(match.group(4))
        den = float(match.group(5))
        return whole + (num / den)

    match_whole = re.search(r'\b(M)?(\d+)SM\b', raw_text)
    if match_whole:
        return float(match_whole.group(2))
    return None


def extract_clouds(raw_text):
    clouds = []
    for match in re.finditer(r'\b(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?\b', raw_text):
        clouds.append({'cover': match.group(1), 'base': int(match.group(2)) * 100})
    return clouds


def map_navcanada_metar(nc_item, site):
    raw_ob = nc_item.get('text', '')
    return {
        'icaoId': site,
        'reportTime': to_iso(nc_item.get('startValidity') or nc_item.get('date', '')),
        'rawOb': raw_ob,
        'clouds': extract_clouds(raw_ob),
        'visib': extract_visibility(raw_ob),
        'wxString': metar_body(raw_ob),
        'source': 'NAVCAN'
    }


def awc_get(path, params, diag, label):
    """Shared AWC call. Handles 204 (valid request, no data) and surfaces the
    400 body, which is how the post-2025 API reports bad parameters."""
    try:
        res = requests.get(f"{AWC_BASE}/{path}", params=params,
                           headers=AWC_HEADERS, timeout=AWC_TIMEOUT)
    except Exception as e:
        diag.add(label, f"request failed: {e}")
        return []

    if res.status_code == 204:
        return []
    if res.status_code != 200:
        diag.add(label, f"HTTP {res.status_code}: {res.text[:200]}")
        return []

    try:
        data = res.json()
    except ValueError:
        diag.add(label, f"non-JSON response: {res.text[:200]}")
        return []

    if isinstance(data, dict):
        data = data.get('data', [])
    return data if isinstance(data, list) else []


def fetch_awc_metars(ids, diag):
    # Dropped the `_=<timestamp>` cache-buster: the Sept 2025 AWC API rework
    # removed undocumented options and now 400s on unknown parameters.
    params = {
        "ids": ",".join(ids),
        "format": "json",
        "taf": "false",
        "hours": 3,
    }
    return awc_get("metar", params, diag, "AWC METAR")


def fetch_navcanada_metars(ids, diag):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://plan.navcanada.ca/wxrecall/'
    }
    url = f"https://plan.navcanada.ca/weather/api/alpha/?site={','.join(ids)}&alpha=metar"
    results = []
    try:
        res = requests.get(url, headers=headers, timeout=8, verify=False)
        if res.status_code != 200:
            diag.add("NAVCAN METAR", f"HTTP {res.status_code}")
            return []
        json_resp = res.json()
        data_list = json_resp.get('data', []) if isinstance(json_resp, dict) else (json_resp or [])
        if not isinstance(data_list, list):
            data_list = []

        for item in data_list:
            raw_ob = item.get('text', '')
            site = item.get('site')
            if not site:
                m = re.search(r'\b(PG[A-Z]{2})\b', raw_ob)
                site = m.group(1) if m else None
            if site and site in ids:
                results.append(map_navcanada_metar(item, site))
    except Exception as e:
        diag.add("NAVCAN METAR", str(e))
    return results


def get_weather_data(diag):
    main_results = []
    aux_results = []
    all_ids = [a['id'] for a in MAIN_AIRPORTS] + [a['id'] for a in AUX_AIRPORTS]

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_awc = executor.submit(fetch_awc_metars, all_ids, diag)
        future_nc = executor.submit(fetch_navcanada_metars, all_ids, diag)
        awc_data = future_awc.result()
        nc_data = future_nc.result()

    combined_data = awc_data + nc_data

    def get_best_report(code):
        reports = [r for r in combined_data if r.get('icaoId') == code]
        if not reports:
            return None

        def sort_key(rep):
            t = parse_ddhhmm_from_text(rep.get('rawOb') or '')
            return t if t else to_iso(rep.get('reportTime', ''))

        reports.sort(key=sort_key, reverse=True)
        return reports[0]

    def process_airport(code, name):
        found = get_best_report(code)
        if not found:
            return {
                "id": code, "name": name, "raw": "WAITING FOR DATA...",
                "time": "", "isoTime": "",
                "pirep_needed": False, "reason": "NO DATA", "is_ifr": False,
                "status": "offline"
            }

        is_needed, reason = check_pirep_condition(found)
        is_ifr = check_ifr_status(found)

        if is_ifr:
            is_needed = True
            if "IFR CONDITIONS" not in reason:
                reason = "IFR CONDITIONS" if reason == "PIREP NOT REQUIRED" else f"IFR CONDITIONS • {reason}"

        raw_ob = found.get('rawOb') or ''
        final_time = parse_ddhhmm_from_text(raw_ob) or to_iso(found.get('reportTime', ''))

        return {
            "id": code, "name": name, "raw": raw_ob,
            "time": final_time, "isoTime": final_time,
            "pirep_needed": is_needed, "reason": reason, "is_ifr": is_ifr,
            "status": "online", "source": found.get('source', 'AWC')
        }

    for apt in MAIN_AIRPORTS:
        main_results.append(process_airport(apt['id'], apt['name']))
    for apt in AUX_AIRPORTS:
        aux_results.append(process_airport(apt['id'], apt['name']))

    return main_results, aux_results


# --- FLASK ROUTES ---

@app.route('/')
def index():
    resp = make_response(render_template('CE_PIREP_INDEX.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


# --- PIREP FETCHING LOGIC ---

def parse_pirep_fields(raw_text):
    acft_str, fl_str = "UNK", "UNK"
    if not raw_text:
        return acft_str, fl_str

    tp_match = re.search(r'/TP\s+([A-Z0-9\-/]+)', raw_text.upper())
    if tp_match:
        acft_str = tp_match.group(1).split('/')[0]

    fl_match = re.search(r'/FL\s*([A-Z0-9]+)', raw_text.upper())
    if fl_match:
        val_str = fl_match.group(1)
        if "DURC" in val_str: fl_str = "DURING CLIMB"
        elif "DURD" in val_str: fl_str = "DURING DESCENT"
        elif val_str.isdigit(): fl_str = f"FL{int(val_str):03d}"
        else: fl_str = val_str

    if fl_str == "UNK":
        if "DURC" in raw_text.upper(): fl_str = "DURING CLIMB"
        elif "DURD" in raw_text.upper(): fl_str = "DURING DESCENT"

    return acft_str, fl_str


def fetch_awc_pireps(diag):
    # The endpoint is /api/data/pirep. There is no /api/data/aircraftreport,
    # and pirep takes id + distance (statute miles) -- it has no bbox param.
    params = {
        "id": "PGUM",
        "distance": 300,
        "age": 2,
        "format": "json",
    }
    raw = awc_get("pirep", params, diag, "AWC PIREP")

    # Real /api/data/pirep fields (confirmed via diagnose.py): rawOb, obsTime,
    # receiptTime, acType, fltLvl, pirepType. There is no reportTime, rawRep,
    # aircraftId, actype, fltlvl, or alt -- those were guesses that never hit.
    reports = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        raw_rep = p.get('rawOb') or ''
        if not raw_rep:
            continue

        t = p.get('obsTime') or p.get('receiptTime')
        acft = p.get('acType')

        # fltLvlType is "DURC"/"DURD" for during-climb/descent reports, but
        # fltLvl can still carry a real altitude alongside that (e.g. "FL015"
        # on short final while classified DURD) -- only fall back to the
        # phase-of-flight label when fltLvl is the 0 filler AWC sends for
        # "no altitude given".
        fl_str = "UNK"
        fl_val = p.get('fltLvl')
        fl_type = (p.get('fltLvlType') or '').upper()
        if fl_val:
            try:
                fl_str = f"FL{int(fl_val):03d}"
            except (ValueError, TypeError):
                pass
        elif fl_type == 'DURC':
            fl_str = "DURING CLIMB"
        elif fl_type == 'DURD':
            fl_str = "DURING DESCENT"

        p_acft, p_fl = parse_pirep_fields(raw_rep)
        reports.append({
            "raw": raw_rep,
            "time": to_iso(t),
            "type": "UUA" if "urgent" in (p.get('pirepType') or '').lower() else "UA",
            "acft": acft or p_acft,
            "fl": fl_str if fl_str != "UNK" else p_fl,
            "source": "AWC"
        })
    return reports


def fetch_navcanada_pireps(diag):
    reports = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://plan.navcanada.ca/wxrecall/'
    }
    url = "https://plan.navcanada.ca/weather/api/alpha/?site=PGUM&radius=300&alpha=pirep"
    try:
        res = requests.get(url, headers=headers, timeout=8, verify=False)
        if res.status_code != 200:
            diag.add("NAVCAN PIREP", f"HTTP {res.status_code}")
            return []

        json_resp = res.json()
        data_list = json_resp.get('data', []) if isinstance(json_resp, dict) else (json_resp or [])
        if not isinstance(data_list, list):
            data_list = []

        for item in data_list:
            raw_text = item.get('text', '')
            if not raw_text:
                continue

            raw_time = item.get('startValidity') or item.get('date') \
                or datetime.now(timezone.utc).isoformat()

            acft, fl = parse_pirep_fields(raw_text)
            reports.append({
                "raw": raw_text,
                "time": to_iso(raw_time),
                "type": "UUA" if "UUA" in raw_text.upper() else "UA",
                "acft": acft, "fl": fl, "source": "NAVCAN"
            })
    except Exception as e:
        diag.add("NAVCAN PIREP", str(e))

    return reports


def normalize_pirep_text(text):
    if not text:
        return ""
    text_upper = text.upper()
    match = re.search(r'\b(UA|UUA)\b', text_upper)
    core_text = text_upper[match.start():] if match else text_upper
    return re.sub(r'[^A-Z0-9]', '', core_text)


@app.route('/api/data')
def api_data():
    diag = Diag()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_weather = executor.submit(get_weather_data, diag)
        future_awc_pirep = executor.submit(fetch_awc_pireps, diag)
        future_nc_pirep = executor.submit(fetch_navcanada_pireps, diag)

        main_metars, aux_metars = future_weather.result()
        awc_data = future_awc_pirep.result()
        nc_data = future_nc_pirep.result()

    combined = {}
    for r in awc_data:
        combined[normalize_pirep_text(r['raw'])] = r

    for r in nc_data:
        key = normalize_pirep_text(r['raw'])
        if key not in combined:
            combined[key] = r

    filtered_pireps = []
    max_age_seconds = 65 * 60
    now_ts = time.time()

    for p in combined.values():
        try:
            t_str = p['time']
            if t_str.endswith('Z'):
                t_str = t_str.replace('Z', '+00:00')
            p_dt = datetime.fromisoformat(t_str)
            if p_dt.tzinfo is None:
                p_dt = p_dt.replace(tzinfo=timezone.utc)
            if (now_ts - p_dt.timestamp()) <= max_age_seconds:
                filtered_pireps.append(p)
        except Exception:
            filtered_pireps.append(p)

    final_pireps = filtered_pireps
    final_pireps.sort(key=lambda x: x.get('time') or '', reverse=True)

    return jsonify({
        "metars": main_metars,
        "aux_metars": aux_metars,
        "pireps": final_pireps,
        "diag": diag.errors,   # empty list == every upstream call was healthy
        "generated": datetime.now(timezone.utc).isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003, debug=True, threaded=True)
