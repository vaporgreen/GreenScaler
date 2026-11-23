"""OptiScaler Wiki + curated game profile database, Steam Proton inspector, and ProtonDB lookup.

Pure Python 3 stdlib; zero external dependencies and safe to import outside Decky.
"""

import json
import os
import re
import time
import unicodedata

PROFILES_URL = "https://raw.githubusercontent.com/vaporgreen/GreenScaler/main/py_modules/greenscaler/data/profiles.json"
PROTONDB_URL = "https://www.protondb.com/api/v1/reports/summaries/%s.json"
PROTONDB_TTL = 7 * 24 * 3600
BUNDLED_PROFILES = os.path.join(os.path.dirname(__file__), "data", "profiles.json")

_FSR4_TOOL_RE = re.compile(r"(proton[-_ ]?ge|ge[-_ ]?proton|proton[-_ ]?cachyos|proton[-_ ]?em|proton[-_ ]?sarek)", re.I)
_EXPERIMENTAL_RE = re.compile(r"proton[-_ ]?experimental", re.I)
_OLD_PROTON_RE = re.compile(r"proton[-_ ]?(stable|[1-9](?:\.\d+)?|hotfix)\b", re.I)


def normalize_title(name):
    if not name:
        return ""
    s = name.replace("™", "").replace("®", "").replace("©", "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = s.replace("'", "")
    s = s.replace("–", "-").replace("—", "-").replace("‐", "-")
    s = re.sub(r"\s*\(\d{4}\)\s*$", "", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


class ProfileDB:
    """Loads OptiScaler Wiki + curated Steam AppID profiles with bundled offline fallback."""

    def __init__(self, cache_dir, log=None):
        self.cache_path = os.path.join(cache_dir, "profiles.json")
        self.log = log or (lambda msg: None)
        self._db = None
        self._mtime = None

    def load(self):
        # Prefer whichever of bundled vs cached has a newer generated_at timestamp.
        cached = _read_json(self.cache_path)
        bundled = _read_json(BUNDLED_PROFILES)
        chosen = bundled or {"schema": 1, "count": 0, "by_appid": {}, "aliases": {}, "by_title": {}}
        if isinstance(cached, dict) and cached.get("schema") == 1 and isinstance(cached.get("by_title"), dict):
            if not bundled or str(cached.get("generated_at", "")) >= str(bundled.get("generated_at", "")):
                chosen = cached
        self._db = chosen
        return chosen

    def db(self):
        if self._db is None:
            return self.load()
        return self._db

    def meta(self):
        d = self.db()
        return {
            "count": int(d.get("count") or len(d.get("by_title") or {})),
            "curated": len(d.get("by_appid") or {}),
            "wiki_commit": d.get("wiki_commit") or "",
            "generated_at": d.get("generated_at") or "",
        }

    def refresh(self, curl_fn):
        """Fetches the latest profiles.json from vaporgreen/GreenScaler main branch."""
        raw = curl_fn([PROFILES_URL], timeout=30)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("by_title"), dict):
            raise ValueError("formato inválido de profiles.json")
        _write_json(self.cache_path, data)
        self._db = data
        self.log("updated profiles DB: %d wiki games (commit %s)" % (data.get("count", 0), data.get("wiki_commit", "")))
        return self.meta()

    def lookup(self, appid, name):
        """Merges OptiScaler Wiki entry (by normalized title) with any curated AppID override."""
        d = self.db()
        by_title = d.get("by_title") or {}
        aliases = d.get("aliases") or {}
        by_appid = d.get("by_appid") or {}

        norm = normalize_title(name)
        wiki = by_title.get(norm)
        if wiki is None and norm in aliases:
            wiki = by_title.get(aliases[norm])

        curated = by_appid.get(str(appid))
        if not wiki and not curated:
            return None

        merged = {}
        if wiki:
            merged.update(wiki)
            merged["source"] = "wiki"
        if curated:
            for k, v in curated.items():
                if k == "ini" and isinstance(v, dict):
                    ini = {sec: dict(kv) for sec, kv in (merged.get("ini") or {}).items()}
                    for sec, kv in v.items():
                        ini.setdefault(sec, {}).update(kv)
                    merged["ini"] = ini
                elif k == "env" and isinstance(v, dict):
                    env = dict(merged.get("env") or {})
                    env.update(v)
                    merged["env"] = env
                elif k == "launch_args" and isinstance(v, list):
                    args = list(merged.get("launch_args") or [])
                    for a in v:
                        if a not in args:
                            args.append(a)
                    merged["launch_args"] = args
                else:
                    merged[k] = v
            merged["source"] = "wiki+curated" if wiki else "curated"
        return merged


# ---------------------------------------------------------------- Steam Proton inspection

def _extract_vdf_block(text, key):
    m = re.search(r'"%s"\s*\{' % re.escape(key), text)
    if not m:
        return ""
    i = m.end()
    depth = 1
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[m.end():i - 1]


def list_custom_protons(steam_roots):
    """Returns custom Proton tool names installed under compatibilitytools.d/."""
    found, seen = [], set()
    for root in steam_roots:
        d = os.path.join(root, "compatibilitytools.d")
        if not os.path.isdir(d):
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            if name in seen or name.startswith(".") or "runtime" in name.lower():
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p) and (os.path.isfile(os.path.join(p, "compatibilitytool.vdf"))
                                     or os.path.isfile(os.path.join(p, "proton"))):
                seen.add(name)
                found.append(name)
    return found


def read_proton_mapping(steam_roots):
    """Parses Steam's config/config.vdf CompatToolMapping -> ({appid: tool_name}, default_tool)."""
    mapping = {}
    for root in steam_roots:
        p = os.path.join(root, "config", "config.vdf")
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        block = _extract_vdf_block(text, "CompatToolMapping")
        if not block:
            continue
        for m in re.finditer(r'"(\d+)"\s*\{([^}]*)\}', block, re.S):
            aid, body = m.group(1), m.group(2)
            nm = re.search(r'"name"\s+"([^"]*)"', body)
            if nm and nm.group(1):
                mapping[aid] = nm.group(1)
        if mapping:
            break
    default_tool = mapping.get("0", "")
    return mapping, default_tool


def classify_proton(tool_name, custom_protons, is_default=False):
    """Assesses whether the game's assigned Proton is ready for FSR 4 / ANR FG."""
    fsr4_installed = [t for t in custom_protons if _FSR4_TOOL_RE.search(t)]
    suggest = " / ".join(fsr4_installed[:2]) if fsr4_installed else "Proton-GE ou Proton-CachyOS"

    if not tool_name:
        return {
            "tool": None,
            "label": "Padrão do Steam",
            "fsr4_ready": False,
            "level": "warn",
            "hint": "Sem Proton customizado fixado. Para FSR 4, selecione %s em Propriedades → Compatibilidade." % suggest,
        }

    suffix = " (padrão)" if is_default else ""
    if _FSR4_TOOL_RE.search(tool_name):
        return {
            "tool": tool_name,
            "label": tool_name + suffix,
            "fsr4_ready": True,
            "level": "ok",
            "hint": "Ótimo para FSR 4 e ANR FG.",
        }
    if _EXPERIMENTAL_RE.search(tool_name):
        return {
            "tool": tool_name,
            "label": "Proton Experimental" + suffix,
            "fsr4_ready": True,
            "level": "ok",
            "hint": "Experimental suporta vkd3d recente; se o FSR 4 não aparecer, teste %s." % suggest,
        }
    if _OLD_PROTON_RE.search(tool_name):
        return {
            "tool": tool_name,
            "label": tool_name + suffix,
            "fsr4_ready": False,
            "level": "warn",
            "hint": "Pode não ter suporte completo ao FSR 4. Recomendado: %s." % suggest,
        }
    return {
        "tool": tool_name,
        "label": tool_name + suffix,
        "fsr4_ready": True,
        "level": "info",
        "hint": "Compatibilidade customizada detectada.",
    }


# ---------------------------------------------------------------- ProtonDB summary (cached)

def fetch_protondb(appid, cache_dir, curl_fn):
    """Fetches & caches ProtonDB summary for an AppID (7-day TTL). Never raises on 404/network."""
    appid = str(appid)
    if not appid.isdigit():
        return {"ok": False, "tier": None}
    path = os.path.join(cache_dir, "protondb", appid + ".json")
    cached = _read_json(path)
    if isinstance(cached, dict) and time.time() - cached.get("_ts", 0) < PROTONDB_TTL:
        return {k: v for k, v in cached.items() if k != "_ts"}
    try:
        raw = curl_fn([PROTONDB_URL % appid], timeout=12)
        data = json.loads(raw.decode("utf-8"))
        out = {
            "ok": True,
            "tier": data.get("tier") or "unknown",
            "trending": data.get("trendingTier") or None,
            "best": data.get("bestReportedTier") or None,
            "total": int(data.get("total") or 0),
            "confidence": data.get("confidence") or "",
        }
        _write_json(path, dict(out, _ts=int(time.time())))
        return out
    except Exception:
        return {"ok": False, "tier": None}
