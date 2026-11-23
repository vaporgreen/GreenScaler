"""GreenScaler core: installs OptiScaler nightly with ANR FG (Apex Neural Rendering) as its add-on, in Proton games.

No dependency on the `decky` module, so it can be run and tested from a plain shell:
    python3 -m greenscaler.core list
OptiScaler comes from optiscaler/OptiScaler-nightly (.7z, checked against the release's sha256 digest) and
ANR FG from apex-compute/ANR's add-on package (checked against its SHA256SUMS.txt). The add-on goes in as
OptiScaler's custom frame generation provider, the way ANR's own add-on install.sh does it.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from urllib.parse import unquote

from .profiles import (
    ProfileDB,
    classify_proton,
    fetch_protondb,
    list_custom_protons,
    read_proton_mapping,
)

ANR_REPO = "apex-compute/ANR"
OPTI_REPO = "optiscaler/OptiScaler-nightly"
REF_REPO = "praydog/REFramework-nightly"
SL_REPO = "NVIDIA-RTX/Streamline"
MFG_REPO = "dashdogy/MFG-Unlocker"
ANR_FOLDER = "Apex Neural Rendering FG"
PROXIES = ("dxgi.dll", "winmm.dll", "version.dll")
LAYOUT = 2  # 1 was ANR's "full" package (0.1.0); 2 is OptiScaler nightly + the ANR add-on
# Files of the OptiScaler archive that are for a person unpacking it by hand
OPTI_SKIP = {"!! EXTRACT ALL FILES TO GAME FOLDER !!", "setup_linux.sh", "setup_windows.bat"}
CHECK_INTERVAL = 6 * 3600
VK_DELETE = 46  # REFramework's menu key default is Insert (45), the same as OptiScaler's

# Steam apps that are tools, not games: hidden unless something of ours is already in them.
NOT_GAMES = {"993090"}  # Lossless Scaling

SKIP_NAME = re.compile(r"^(proton\b|steam linux runtime|steamworks common|steam\.dll)", re.I)
SKIP_DIRS = {
    "_commonredist", "commonredist", "redist", "redistributables", "__installer", "directx",
    "vcredist", "dotnet", "engine", "reframework", "optiscaler", "easyanticheat", "battleye",
}
SIDE_EXE = re.compile(
    r"(crash|unins|setup|install|redist|launcher|handler|helper|report|vc_|dxsetup|"
    r"easyanticheat|battleye|prereq|start_protected|benchmark|cefprocess|subprocess)", re.I)

DEFAULTS = {
    "fsr4": True,           # Dx12Upscaler=ffx + PROTON_FSR4_UPGRADE=1
    "fsr4_model": "auto",   # auto | 0 FP8 | 1 INT8 | 2 INT8 RDNA2 | 3 INT8 Lite
    "fg": "game",           # off | game (follow the game's menu) | 1..5 generated frames (2X..6X)
    "proxy": "auto",        # auto (wiki/engine) | dxgi.dll | winmm.dll | version.dll
    "streamline": False,    # fetch NVIDIA-RTX/Streamline bin/x64/*.dll into OptiScaler/streamline/
    "mfg_unlocker": False,  # fetch dashdogy/MFG-Unlocker -> OptiScaler/plugins/MFGUnlocker.asi + LoadAsiPlugins=true
    "reframework": True,    # RE Engine games only
    "profile_tweaks": True, # apply OptiScaler Wiki / Proton INI & launch overrides when available
}


class GsError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _clean_env():
    env = dict(os.environ)
    # Decky's bundled Python exports its own library path; system curl must not see it.
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    return env


def _curl(args, timeout=60):
    cmd = ["curl", "-fsSL", "--retry", "2", "--connect-timeout", "15", "-A", "greenscaler-decky"] + args
    try:
        res = subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GsError("tempo esgotado na conexão")
    except FileNotFoundError:
        raise GsError("curl não encontrado no sistema")
    if res.returncode != 0:
        raise GsError("falha de rede (curl %d): %s" % (res.returncode, res.stderr.decode("utf-8", "replace").strip()[:200]))
    return res.stdout


def http_json(url):
    return json.loads(_curl([url]).decode("utf-8"))


def download(url, dest, timeout=900):
    part = dest + ".part"
    _curl(["-o", part, url], timeout=timeout)
    os.replace(part, dest)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def extract_7z(archive, dest):
    os.makedirs(dest, exist_ok=True)
    for cmd in (["7z", "x", "-y", "-o" + dest, archive], ["7za", "x", "-y", "-o" + dest, archive],
                ["bsdtar", "-xf", archive, "-C", dest]):
        try:
            res = subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=600)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            raise GsError("tempo esgotado ao extrair o OptiScaler")
        if res.returncode == 0:
            break
    else:
        raise GsError("não foi possível extrair o .7z (7z ou bsdtar ausentes, ou arquivo inválido)")
    root = os.path.realpath(dest)
    for cur, dirs, files in os.walk(dest):
        for name in dirs + files:
            p = os.path.join(cur, name)
            if os.path.islink(p) or not os.path.realpath(p).startswith(root + os.sep):
                shutil.rmtree(dest, ignore_errors=True)
                raise GsError("o arquivo do OptiScaler tem um caminho ou link inesperado: " + name)


def copy_over(src, dst):
    """Copies a file or a tree onto dst, replacing files that are there (never following a link at dst)."""
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            copy_over(os.path.join(src, name), os.path.join(dst, name))
    else:
        if os.path.lexists(dst):
            os.remove(dst)
        shutil.copy2(src, dst)


def safe_extract(zip_path, dest):
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = os.path.realpath(os.path.join(root, info.filename))
            if target != root and not target.startswith(root + os.sep):
                raise GsError("pacote com caminho inválido: " + info.filename)
        z.extractall(root)


def is_handheld():
    try:
        return any(n.startswith("BAT") for n in os.listdir("/sys/class/power_supply"))
    except OSError:
        return False


# ---------------------------------------------------------------- OptiScaler.ini

def ini_set(text, section, key, value):
    """Set key in [section], keeping the file's own spacing and line endings. Adds what is missing."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    head = re.compile(r"^\s*\[(.+?)\]\s*$")
    start = None
    for i, line in enumerate(lines):
        m = head.match(line)
        if m and m.group(1).strip().lower() == section.lower():
            start = i
            break
    if start is None:
        return text.rstrip("\r\n") + nl + nl + "[%s]" % section + nl + "%s=%s" % (key, value) + nl
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if head.match(lines[i]):
            end = i
            break
    pat = re.compile(r"^(\s*" + re.escape(key) + r")(\s*=\s*)(.*?)\s*$", re.I)
    for i in range(start + 1, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = m.group(1) + m.group(2) + str(value)
            return nl.join(lines)
    lines.insert(start + 1, "%s=%s" % (key, value))
    return nl.join(lines)


def ini_get(text, section, key):
    in_sec = False
    for line in text.splitlines():
        m = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if m:
            in_sec = m.group(1).strip().lower() == section.lower()
            continue
        if in_sec:
            m = re.match(r"^\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$", line, re.I)
            if m:
                return m.group(1)
    return None


def apply_options(text, opts, extra_ini=None):
    fg = opts.get("fg", "game")
    if opts.get("fsr4"):
        text = ini_set(text, "Upscalers", "Dx12Upscaler", "ffx")
        model = str(opts.get("fsr4_model", "auto"))
        text = ini_set(text, "FSR", "Fsr4Model", model if model in ("0", "1", "2", "3") else "auto")
    if fg == "off":
        text = ini_set(text, "FrameGen", "FGInput", "nofg")
        text = ini_set(text, "DLSSG", "OverrideInterpolationCount", "auto")
    else:
        text = ini_set(text, "FrameGen", "FGInput", "nvngxfg")
        text = ini_set(text, "FrameGen", "FGNvngxReplacement", ANR_FOLDER)
        # ANR FG draws the HUD over the HUD-less color; OptiScaler must not strip it (ANR's add-on sets this too)
        text = ini_set(text, "FrameGen", "DisableHudless", "false")
        count = str(fg) if str(fg) in ("1", "2", "3", "4", "5") else "auto"
        text = ini_set(text, "DLSSG", "OverrideInterpolationCount", count)
    if opts.get("mfg_unlocker"):
        text = ini_set(text, "Plugins", "LoadAsiPlugins", "true")
    if opts.get("profile_tweaks", True) and isinstance(extra_ini, dict):
        for section, kv in extra_ini.items():
            if isinstance(kv, dict):
                for key, val in kv.items():
                    text = ini_set(text, section, key, val)
    return text


# ---------------------------------------------------------------- Steam launch options

_ASSIGN = r'=(?:"(?:[^"\\]|\\.)*"|\S*)'
_MANAGED_ENV = ("PROTON_ENABLE_NVAPI", "PROTON_FSR4_UPGRADE")


def _split_command(existing):
    existing = (existing or "").strip()
    if "%command%" in existing:
        pre, post = existing.split("%command%", 1)
        return pre.strip(), post.rstrip()
    return "", (" " + existing) if existing else ""


def _overrides(pre, drop):
    """Returns (pre without WINEDLLOVERRIDES, the entries of it that are not ours)."""
    kept = []
    m = re.search(r"\bWINEDLLOVERRIDES" + "(" + _ASSIGN + ")", pre)
    if m:
        value = m.group(1)[1:].strip('"').replace('\\"', '"')
        for entry in value.split(";"):
            if "=" not in entry:
                continue
            names, mode = entry.split("=", 1)
            left = [n.strip() for n in names.split(",") if n.strip() and n.strip().lower() not in drop]
            if left:
                kept.append(",".join(left) + "=" + mode.strip())
        pre = (pre[:m.start()] + pre[m.end():]).strip()
    return pre, kept


def _strip_env(pre, names=None):
    for name in (names if names is not None else _MANAGED_ENV):
        pre = re.sub(r"\b" + re.escape(name) + _ASSIGN, "", pre)
    return " ".join(pre.split())


def _strip_post_args(post, args):
    if not args or not post:
        return post
    tokens = post.split()
    drop = set(args)
    kept = [t for t in tokens if t not in drop]
    return (" " + " ".join(kept)) if kept else ""


def merge_launch(existing, proxy, dinput8, fsr4, extra_env=None, extra_args=None, prev_env=None, prev_args=None):
    pre, post = _split_command(existing)
    drop = {p[:-4] for p in PROXIES} | ({"dinput8"} if dinput8 else set())
    pre, kept = _overrides(pre, drop)
    env_to_strip = list(_MANAGED_ENV) + list(prev_env or []) + list((extra_env or {}).keys())
    pre = _strip_env(pre, env_to_strip)
    if prev_args:
        post = _strip_post_args(post, prev_args)
    ours = [proxy[:-4]] + (["dinput8"] if dinput8 else [])
    parts = ['WINEDLLOVERRIDES="%s"' % ";".join(kept + [",".join(ours) + "=n,b"]), "PROTON_ENABLE_NVAPI=1"]
    if fsr4:
        parts.append("PROTON_FSR4_UPGRADE=1")
    for k, v in (extra_env or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)):
            parts.append("%s=%s" % (k, v))
    if pre:
        parts.append(pre)
    existing_post_tokens = set(post.split())
    for arg in (extra_args or []):
        if arg and arg not in existing_post_tokens:
            post = (post + " " + arg) if post else (" " + arg)
    return " ".join(parts) + " %command%" + post


def strip_launch(existing, dinput8, prev_env=None, prev_args=None):
    pre, post = _split_command(existing)
    drop = {p[:-4] for p in PROXIES} | ({"dinput8"} if dinput8 else set())
    pre, kept = _overrides(pre, drop)
    pre = _strip_env(pre, list(_MANAGED_ENV) + list(prev_env or []))
    if prev_args:
        post = _strip_post_args(post, prev_args)
    parts = (['WINEDLLOVERRIDES="%s"' % ";".join(kept)] if kept else []) + ([pre] if pre else [])
    if not parts:
        return post.strip()
    return " ".join(parts) + " %command%" + post


# ---------------------------------------------------------------- the core

class Core:
    def __init__(self, home, data_dir, log=None):
        self.home = home
        self.data = data_dir
        self.log = log or (lambda msg: None)
        for sub in ("opti", "addon"):
            os.makedirs(os.path.join(self.data, sub), exist_ok=True)
        shutil.rmtree(os.path.join(self.data, "anr"), ignore_errors=True)  # 0.1.0's cache of the "full" package
        self.state_path = os.path.join(self.data, "state.json")
        self.settings_path = os.path.join(self.data, "settings.json")
        self.profiles = ProfileDB(self.data, self.log)
        self._fetch_lock = threading.Lock()  # the start-up check and the button can overlap

    def profiles_meta(self):
        return self.profiles.meta()

    def protondb(self, appid):
        return fetch_protondb(appid, self.data, _curl)

    # ---- settings

    def default_options(self):
        d = dict(DEFAULTS)
        if is_handheld():
            d["fg"] = "1"          # 2X: more than that costs too much GPU time on an APU
            d["fsr4_model"] = "3"  # INT8 Lite, the model meant for APUs
        return d

    def settings(self):
        s = read_json(self.settings_path, {}) or {}
        out = {"auto_update": True, "auto_apply": True, "defaults": self.default_options(), "games": {}}
        out["auto_update"] = bool(s.get("auto_update", True))
        out["auto_apply"] = bool(s.get("auto_apply", True))
        out["defaults"].update(s.get("defaults", {}))
        out["games"] = s.get("games", {})
        return out

    def save_settings(self, patch):
        s = self.settings()
        for k in ("auto_update", "auto_apply"):
            if k in patch:
                s[k] = bool(patch[k])
        if "defaults" in patch:
            s["defaults"].update(patch["defaults"])
        write_json(self.settings_path, s)
        return s

    def options_for(self, appid, override=None):
        s = self.settings()
        opts = dict(s["defaults"])
        opts.update(s["games"].get(str(appid), {}))
        if override:
            opts.update(override)
        opts["fg"] = str(opts["fg"])
        opts["fsr4_model"] = str(opts["fsr4_model"])
        return opts

    def _remember(self, appid, opts):
        s = self.settings()
        s["games"][str(appid)] = {k: opts[k] for k in DEFAULTS if k in opts}
        write_json(self.settings_path, s)

    # ---- package cache: OptiScaler nightly (opti/<tag>) and the ANR add-on (addon/<tag>)

    def state(self):
        st = read_json(self.state_path, {}) or {}
        return {"opti": st.get("opti"), "anr": st.get("anr"), "prev": st.get("prev"),
                "pinned": bool(st.get("pinned")), "checked_at": st.get("checked_at", 0)}

    def _save_state(self, **kw):
        st = self.state()
        st.update(kw)
        write_json(self.state_path, st)
        return st

    def _dir(self, kind, tag):
        return os.path.join(self.data, kind, re.sub(r"[^A-Za-z0-9._-]", "_", tag))

    def _has(self, kind, tag):
        return bool(tag) and os.path.isfile(os.path.join(self._dir(kind, tag), ".ok"))

    def ready(self, st=None):
        st = st or self.state()
        return self._has("opti", st["opti"]) and self._has("addon", st["anr"])

    def latest_opti(self):
        """(tag, {name, url, sha256}) of the newest OptiScaler nightly."""
        try:
            rel = http_json("https://api.github.com/repos/%s/releases?per_page=1" % OPTI_REPO)[0]
            asset = next(a for a in rel["assets"] if a["name"].lower().endswith(".7z"))
            digest = asset.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise GsError("o nightly do OptiScaler veio sem sha256")
            return rel["tag_name"], {"name": asset["name"], "url": asset["browser_download_url"], "sha256": digest[7:].lower()}
        except (KeyError, ValueError, IndexError, StopIteration) as e:
            raise GsError("resposta inesperada do GitHub para o OptiScaler nightly (%s)" % type(e).__name__)

    def latest_anr(self):
        """(tag, {name, url, sums_url}) of ANR's newest add-on; uses the redirect when the API is rate-limited."""
        try:
            rel = http_json("https://api.github.com/repos/%s/releases/latest" % ANR_REPO)
            tag = rel["tag_name"]
            assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
        except (GsError, KeyError, ValueError) as e:
            self.log("ANR release API failed (%s), using the redirect" % e)
            out = subprocess.run(
                ["curl", "-sIL", "-o", "/dev/null", "-w", "%{url_effective}", "--connect-timeout", "15",
                 "https://github.com/%s/releases/latest" % ANR_REPO],
                env=_clean_env(), capture_output=True, timeout=40).stdout.decode().strip()
            if "/releases/tag/" not in out:
                raise GsError("não foi possível consultar o release mais novo do ANR")
            tag = unquote(out.rsplit("/", 1)[1])
            ver = tag[3:] if tag.startswith("FG-") else tag
            base = "https://github.com/%s/releases/download/%s/" % (ANR_REPO, tag)
            assets = {n: base + n for n in ("anr-fg-%s-addon.zip" % ver, "SHA256SUMS.txt")}
        name = next((n for n in assets if re.fullmatch(r"anr-fg-.+-addon\.zip", n)), None)
        if not name or "SHA256SUMS.txt" not in assets:
            raise GsError("o release %s do ANR não tem o pacote 'addon' ou o SHA256SUMS.txt" % tag)
        return tag, {"name": name, "url": assets[name], "sums_url": assets["SHA256SUMS.txt"]}

    def _fetch_opti(self, tag, info):
        if self._has("opti", tag):
            return
        d = self._dir("opti", tag)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        archive = os.path.join(d, "pkg.7z")
        download(info["url"], archive)
        if sha256(archive) != info["sha256"]:
            shutil.rmtree(d, ignore_errors=True)
            raise GsError("o OptiScaler baixado não confere com o sha256 do release")
        write_json(os.path.join(d, ".ok"), {"tag": tag, "name": info["name"], "sha256": info["sha256"]})
        self.log("fetched OptiScaler %s" % tag)

    def _fetch_addon(self, tag, info):
        if self._has("addon", tag):
            return
        d = self._dir("addon", tag)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        sums = _curl([info["sums_url"]]).decode("utf-8", "replace")
        want = None
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == info["name"]:
                want = parts[0].lower()
        if not want:
            raise GsError("SHA256SUMS.txt não lista " + info["name"])
        zpath = os.path.join(d, info["name"])
        download(info["url"], zpath)
        if sha256(zpath) != want:
            shutil.rmtree(d, ignore_errors=True)
            raise GsError("o add-on do ANR baixado não confere com o SHA256 publicado")
        safe_extract(zpath, d)
        os.remove(zpath)
        if not os.path.isfile(os.path.join(d, ANR_FOLDER, "dlssg", "nvngx_dlssg.dll")):
            shutil.rmtree(d, ignore_errors=True)
            raise GsError("add-on sem nvngx_dlssg.dll: o formato do release do ANR mudou")
        write_json(os.path.join(d, ".ok"), {"tag": tag, "sha256": want})
        self.log("fetched ANR add-on %s" % tag)

    def _opti_tree(self, tag):
        """The unpacked OptiScaler of a cached nightly (unpacked on first use; only the current one stays unpacked)."""
        d = self._dir("opti", tag)
        tree = os.path.join(d, "x")
        if not os.path.isfile(os.path.join(tree, "OptiScaler.dll")):
            shutil.rmtree(tree, ignore_errors=True)
            extract_7z(os.path.join(d, "pkg.7z"), tree)
            if not os.path.isfile(os.path.join(tree, "OptiScaler.dll")) or not os.path.isfile(os.path.join(tree, "OptiScaler.ini")):
                shutil.rmtree(tree, ignore_errors=True)
                raise GsError("OptiScaler nightly sem OptiScaler.dll/ini na raiz: o formato do pacote mudou")
        return tree

    def _prune(self, st):
        prev = st.get("prev") or {}
        keep = {"opti": {st.get("opti"), prev.get("opti")}, "addon": {st.get("anr"), prev.get("anr")}}
        for kind, tags in keep.items():
            dirs = {self._dir(kind, t) for t in tags if t}
            root = os.path.join(self.data, kind)
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if os.path.isdir(p) and p not in dirs:
                    shutil.rmtree(p, ignore_errors=True)
        if st.get("opti"):
            cur = self._dir("opti", st["opti"])
            for name in os.listdir(os.path.join(self.data, "opti")):
                p = os.path.join(self.data, "opti", name)
                if p != cur:
                    shutil.rmtree(os.path.join(p, "x"), ignore_errors=True)  # keep the 70 MB archive, not 280 MB unpacked

    def check_update(self, force=False):
        """Looks for a newer OptiScaler nightly and ANR add-on and caches them. Returns the state plus `changed`."""
        with self._fetch_lock:
            st = self.state()
            if not force and self.ready(st) and (st["pinned"] or time.time() - st["checked_at"] < CHECK_INTERVAL):
                return dict(st, changed=False)
            opti_tag, opti_info = self.latest_opti()
            anr_tag, anr_info = self.latest_anr()
            self._fetch_opti(opti_tag, opti_info)
            self._fetch_addon(anr_tag, anr_info)
            changed = (opti_tag, anr_tag) != (st["opti"], st["anr"])
            if changed:
                prev = {"opti": st["opti"], "anr": st["anr"]} if self.ready(st) else st.get("prev")
                st = self._save_state(opti=opti_tag, anr=anr_tag, prev=prev, pinned=False, checked_at=int(time.time()))
            else:
                st = self._save_state(checked_at=int(time.time()), pinned=False if force else st["pinned"])
            self._prune(st)
            try:
                self.profiles.refresh(_curl)
            except Exception as e:
                self.log("profiles DB refresh skipped: %s" % e)
            return dict(st, changed=changed)

    def rollback(self):
        """Swaps to the pair of versions in use before the last update and pauses automatic updates."""
        with self._fetch_lock:
            st = self.state()
            prev = st.get("prev") or {}
            if not (self._has("opti", prev.get("opti")) and self._has("addon", prev.get("anr"))):
                raise GsError("não há versão anterior guardada")
            st = self._save_state(opti=prev["opti"], anr=prev["anr"], prev={"opti": st["opti"], "anr": st["anr"]}, pinned=True)
            self._prune(st)
            return st

    # ---- games

    def steam_roots(self):
        seen, out = set(), []
        for rel in (".local/share/Steam", ".steam/steam", ".steam/root", ".steam/debian-installation"):
            p = os.path.realpath(os.path.join(self.home, rel))
            if p not in seen and os.path.isdir(os.path.join(p, "steamapps")):
                seen.add(p)
                out.append(p)
        return out

    def libraries(self):
        seen, out = set(), []
        for root in self.steam_roots():
            cands = [root]
            try:
                with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), "r", encoding="utf-8", errors="replace") as f:
                    cands += [p.replace("\\\\", "/") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
            except OSError:
                pass
            for c in cands:
                p = os.path.realpath(os.path.join(c, "steamapps"))
                if p not in seen and os.path.isdir(p):
                    seen.add(p)
                    out.append(p)
        return out

    @staticmethod
    def _acf(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
        def val(key):
            m = re.search(r'"%s"\s+"([^"]*)"' % key, text)
            return m.group(1) if m else None
        if not val("appid") or not val("installdir"):
            return None
        return {"appid": val("appid"), "name": val("name") or val("installdir"), "installdir": val("installdir")}

    @staticmethod
    def _scan(root):
        """One bounded walk of the game folder: the .exe files, upscaler/anti-cheat markers and any install of ours."""
        exes = []
        marks = {
            "dlss": False,
            "dlssg": False,
            "streamline": False,
            "fsr": False,
            "xess": False,
            "anticheat": False,
            "record": None,
        }
        base = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            depth = cur.count(os.sep) - base
            low = {d.lower() for d in dirs}
            if low & {"easyanticheat", "battleye", "eaanticheat", "vanguard"}:
                marks["anticheat"] = True
            if "OptiScaler" in dirs and any(os.path.isfile(os.path.join(cur, "OptiScaler", n))
                                            for n in ("greenscaler.json", "nr_fg_install.json")):
                marks["record"] = cur
            dirs[:] = [] if depth >= 5 else [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                lname = name.lower()
                if lname in ("nvngx_dlss.dll", "sl.dlss.dll"):
                    marks["dlss"] = True
                elif lname in ("nvngx_dlssg.dll", "sl.dlss_g.dll"):
                    marks["dlssg"] = True
                elif lname in ("sl.interposer.dll", "sl.common.dll"):
                    marks["streamline"] = True
                elif lname.startswith(("amd_fidelityfx_", "ffx_fsr")) and lname.endswith(".dll"):
                    marks["fsr"] = True
                elif lname in ("libxess.dll", "libxess_dx11.dll", "libxess_fg.dll"):
                    marks["xess"] = True
                elif lname in ("start_protected_game.exe", "eac_launcher.exe", "eaanticheat.gameservicelauncher.exe"):
                    marks["anticheat"] = True
                elif lname.endswith(".exe"):
                    try:
                        exes.append((os.path.getsize(os.path.join(cur, name)), cur, name))
                    except OSError:
                        pass
        # Unreal Engine 4/5 places DLSS/Streamline/XeSS/FSR plugins inside Engine/Plugins/ or <Game>/Plugins/ (depth 6-8).
        plugin_roots = []
        eng_plugins = os.path.join(root, "Engine", "Plugins")
        if os.path.isdir(eng_plugins):
            plugin_roots.append(eng_plugins)
        try:
            for d in os.listdir(root):
                cand = os.path.join(root, d, "Plugins")
                if d.lower() != "engine" and os.path.isdir(cand):
                    plugin_roots.append(cand)
        except OSError:
            pass
        for pr in plugin_roots:
            pbase = pr.rstrip(os.sep).count(os.sep)
            for cur, dirs, files in os.walk(pr):
                pdepth = cur.count(os.sep) - pbase
                if pdepth >= 6:
                    dirs[:] = []
                low_dirs = {d.lower() for d in dirs}
                if "dlss" in low_dirs:
                    marks["dlss"] = True
                if "streamline" in low_dirs:
                    marks["streamline"] = True
                if "xess" in low_dirs:
                    marks["xess"] = True
                if any(d.startswith("fsr") for d in low_dirs):
                    marks["fsr"] = True
                for name in files:
                    lname = name.lower()
                    if lname in ("nvngx_dlss.dll", "sl.dlss.dll"):
                        marks["dlss"] = True
                    elif lname in ("nvngx_dlssg.dll", "sl.dlss_g.dll"):
                        marks["dlssg"] = True
                    elif lname in ("sl.interposer.dll", "sl.common.dll"):
                        marks["streamline"] = True
                    elif lname.startswith(("amd_fidelityfx_", "ffx_fsr")) and lname.endswith(".dll"):
                        marks["fsr"] = True
                    elif lname in ("libxess.dll", "libxess_dx11.dll", "libxess_fg.dll"):
                        marks["xess"] = True
        return exes, marks

    @staticmethod
    def _options_from_ini(path):
        try:
            with open(path, "r", encoding="latin-1") as f:
                text = f.read()
        except OSError:
            return None
        get = lambda sec, key: (ini_get(text, sec, key) or "auto").strip().lower()
        count = get("DLSSG", "OverrideInterpolationCount")
        fg = "off" if get("FrameGen", "FGInput") == "nofg" else (count if count in ("1", "2", "3", "4", "5") else "game")
        model = get("FSR", "Fsr4Model")
        return {"fsr4": get("Upscalers", "Dx12Upscaler") == "ffx", "fg": fg,
                "fsr4_model": model if model in ("0", "1", "2", "3") else "auto"}

    def _recommended_options(self, marks, re_engine, profile, exe_path):
        rec = self.default_options()
        rec["proxy"] = "auto"
        if profile:
            if profile.get("proxy") in PROXIES:
                rec["proxy"] = profile["proxy"]
            if profile.get("streamline"):
                rec["streamline"] = True
            if profile.get("reframework"):
                rec["reframework"] = True
            if profile.get("status") == "broken":
                rec["fg"] = "off"
        if marks.get("streamline") and marks.get("dlssg"):
            rec["streamline"] = True
        if not marks.get("dlssg") and not (profile and profile.get("streamline")):
            rec["fg"] = "off"
        if re_engine:
            rec["reframework"] = True
        if rec["proxy"] == "auto":
            detected_proxy = self._detect_proxy(exe_path)
            if detected_proxy != "dxgi.dll":
                rec["proxy"] = detected_proxy
        rec["profile_tweaks"] = True
        return rec

    def _describe(self, lib, acf, proton_ctx=None):
        root = os.path.join(lib, "common", acf["installdir"])
        if SKIP_NAME.match(acf["name"]) or not os.path.isdir(root):
            return None
        exes, marks = self._scan(root)
        if not exes:
            return None
        if marks["record"]:
            exe_dir = marks["record"]
            here = [e for e in exes if e[1] == exe_dir] or exes
        else:
            main = [e for e in exes if not SIDE_EXE.search(e[2])] or exes
            exe_dir = max(main)[1]
            here = [e for e in main if e[1] == exe_dir]
        exe = max(here)[2]
        opti = os.path.join(exe_dir, "OptiScaler")
        meta = read_json(os.path.join(opti, "greenscaler.json"), {}) or {}
        managed = meta.get("layout") == LAYOUT
        # An install of ANR's "full" package (by hand, or by 0.1.0 of this plugin): taken over on the next apply.
        legacy = not managed and os.path.isfile(os.path.join(opti, "nr_fg_install.json"))
        installed = managed or legacy
        if acf["appid"] in NOT_GAMES and not installed:
            return None
        foreign = not installed and (os.path.isdir(opti) or os.path.isfile(os.path.join(exe_dir, "OptiScaler.ini")))
        st = self.state()
        current = meta.get("options") if managed else None
        if legacy:
            current = self._options_from_ini(os.path.join(exe_dir, "OptiScaler.ini"))
        re_engine = any(n.startswith("re_chunk_000.pak") for n in os.listdir(root))
        profile = self.profiles.lookup(acf["appid"], acf["name"])
        anticheat = bool(marks["anticheat"] or (profile and profile.get("anticheat")))
        if proton_ctx is None:
            roots = self.steam_roots()
            proton_ctx = (read_proton_mapping(roots), list_custom_protons(roots))
        (pmap, pdef), custom_protons = proton_ctx
        ptool = pmap.get(str(acf["appid"]))
        proton_info = classify_proton(ptool or pdef, custom_protons, is_default=not bool(ptool) and bool(pdef))
        recommended = self._recommended_options(marks, re_engine, profile, os.path.join(exe_dir, exe))
        return {
            "appid": acf["appid"], "name": acf["name"], "root": root, "exe_dir": exe_dir, "exe": exe,
            "installed": installed, "managed": managed, "legacy": legacy, "foreign": foreign,
            "opti": meta.get("opti") if managed else None, "anr": meta.get("anr") if managed else None,
            "proxy": meta.get("proxy") if managed else None,
            "outdated": managed and (meta.get("opti"), meta.get("anr")) != (st["opti"], st["anr"]),
            "has_dlss": marks["dlss"], "has_dlssg": marks["dlssg"],
            "has_streamline": marks["streamline"], "has_fsr": marks["fsr"], "has_xess": marks["xess"],
            "anticheat": anticheat,
            "re_engine": re_engine,
            "has_reframework": os.path.isfile(os.path.join(exe_dir, "dinput8.dll")),
            "proton": proton_info,
            "profile": profile,
            "recommended": recommended,
            "options": self.options_for(acf["appid"], current),
        }

    def games(self):
        out, seen = [], set()
        roots = self.steam_roots()
        proton_ctx = (read_proton_mapping(roots), list_custom_protons(roots))
        for lib in self.libraries():
            try:
                names = sorted(os.listdir(lib))
            except OSError:
                continue
            for name in names:
                if not (name.startswith("appmanifest_") and name.endswith(".acf")):
                    continue
                acf = self._acf(os.path.join(lib, name))
                if not acf or acf["appid"] in seen:
                    continue
                g = self._describe(lib, acf, proton_ctx)
                if g:
                    seen.add(acf["appid"])
                    out.append(g)
        return sorted(out, key=lambda g: g["name"].lower())

    def game(self, appid):
        for g in self.games():
            if g["appid"] == str(appid):
                return g
        raise GsError("jogo %s não encontrado" % appid)

    # ---- install / uninstall

    @staticmethod
    def _running(exe_dir):
        for name in os.listdir(exe_dir):
            if name.lower().endswith(".exe"):
                r = subprocess.run(["pgrep", "-f", "--", re.escape(name)], env=_clean_env(), capture_output=True)
                if r.returncode == 0:
                    return name
        return None

    @staticmethod
    def _detect_proxy(exe_path):
        # A Vulkan game that never loads DXGI needs winmm.dll (same test as ANR's install.sh).
        try:
            with open(exe_path, "rb") as f:
                data = f.read().replace(b"\x00", b"").lower()
        except OSError:
            return "dxgi.dll"
        if b"vulkan-1.dll" in data and not re.search(rb"dxgi\.dll|d3d1[12]\.dll", data):
            return "winmm.dll"
        return "dxgi.dll"

    def _streamline(self, opti_dir, state):
        """Linux port of OptiScaler's Streamlined_fetcher_windows.bat.
        Downloads the latest NVIDIA-RTX/Streamline SDK zip once into cache, extracts all
        bin/x64/*.dll files, and copies them to <game>/OptiScaler/streamline/."""
        rel = http_json("https://api.github.com/repos/%s/releases/latest" % SL_REPO)
        tag = rel["tag_name"]
        asset = next((a for a in rel.get("assets", []) if re.match(r"^streamline-sdk-v[0-9.]+\.zip$", a["name"], re.I)), None)
        if not asset:
            raise GsError("o release %s do Streamline não tem streamline-sdk-v*.zip" % tag)
        cache_dir = self._dir("streamline", tag)
        if not os.path.isfile(os.path.join(cache_dir, ".ok")):
            shutil.rmtree(cache_dir, ignore_errors=True)
            os.makedirs(cache_dir, exist_ok=True)
            zpath = os.path.join(cache_dir, "release.zip")
            download(asset["browser_download_url"], zpath)
            dlls = 0
            try:
                with zipfile.ZipFile(zpath) as z:
                    for info in z.infolist():
                        norm = info.filename.replace("\\", "/")
                        low = norm.lower()
                        if info.is_dir() or not low.endswith(".dll"):
                            continue
                        if "/bin/x64/" not in low and not low.startswith("bin/x64/"):
                            continue
                        base = os.path.basename(norm)
                        if not base:
                            continue
                        out_path = os.path.join(cache_dir, base)
                        with z.open(info) as src, open(out_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        dlls += 1
            finally:
                if os.path.exists(zpath):
                    os.remove(zpath)
            if dlls == 0:
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise GsError("nenhuma DLL encontrada em bin/x64 dentro do pacote Streamline")
            write_json(os.path.join(cache_dir, ".ok"), {"tag": tag, "asset": asset["name"], "dlls": dlls})
            self.log("fetched Streamline SDK %s (%d DLLs)" % (tag, dlls))

        dest = os.path.join(opti_dir, "streamline")
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(cache_dir):
            if name.lower().endswith(".dll"):
                copy_over(os.path.join(cache_dir, name), os.path.join(dest, name))
        state["streamline"] = tag
        return tag

    def _mfg_unlocker(self, opti_dir, state):
        """Downloads dashdogy/MFG-Unlocker latest zip once into cache, verifies SHA256SUMS.txt,
        and installs MFGUnlocker.dll as <game>/OptiScaler/plugins/MFGUnlocker.asi."""
        rel = http_json("https://api.github.com/repos/%s/releases/latest" % MFG_REPO)
        tag = rel["tag_name"]
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        zip_name = next((n for n in assets if re.match(r"^MFGUnlocker-v?[0-9.]+\.zip$", n, re.I)), None)
        if not zip_name:
            raise GsError("o release %s do MFG-Unlocker não tem MFGUnlocker-*.zip" % tag)
        cache_dir = self._dir("mfg_unlocker", tag)
        cached_dll = os.path.join(cache_dir, "MFGUnlocker.dll")
        if not (os.path.isfile(os.path.join(cache_dir, ".ok")) and os.path.isfile(cached_dll)):
            shutil.rmtree(cache_dir, ignore_errors=True)
            os.makedirs(cache_dir, exist_ok=True)
            want_zip = want_dll = None
            if "SHA256SUMS.txt" in assets:
                sums = _curl([assets["SHA256SUMS.txt"]]).decode("utf-8", "replace")
                for line in sums.splitlines():
                    parts = line.split()
                    if len(parts) == 2:
                        fname = parts[1].lstrip("*")
                        if fname == zip_name:
                            want_zip = parts[0].lower()
                        elif fname.lower() == "mfgunlocker.dll":
                            want_dll = parts[0].lower()
            zpath = os.path.join(cache_dir, zip_name)
            download(assets[zip_name], zpath)
            if want_zip and sha256(zpath) != want_zip:
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise GsError("o MFG-Unlocker baixado não confere com o SHA256 publicado")
            found = False
            try:
                with zipfile.ZipFile(zpath) as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        if os.path.basename(info.filename.replace("\\", "/")).lower() == "mfgunlocker.dll":
                            with z.open(info) as src, open(cached_dll, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            found = True
                            break
            finally:
                if os.path.exists(zpath):
                    os.remove(zpath)
            if not found:
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise GsError("MFGUnlocker.dll não encontrado dentro de %s" % zip_name)
            if want_dll and sha256(cached_dll) != want_dll:
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise GsError("MFGUnlocker.dll extraído não confere com o SHA256 publicado")
            write_json(os.path.join(cache_dir, ".ok"), {"tag": tag, "asset": zip_name, "sha256": sha256(cached_dll)})
            self.log("fetched MFG-Unlocker %s" % tag)

        plugins_dir = os.path.join(opti_dir, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        copy_over(cached_dll, os.path.join(plugins_dir, "MFGUnlocker.asi"))
        state["mfg_unlocker"] = tag
        return tag

    def _reframework(self, g, state):
        """Puts REFramework nightly's dinput8.dll next to the game. Returns the tag installed, or None."""
        dest = os.path.join(g["exe_dir"], "dinput8.dll")
        ours = state.get("reframework_sha")
        if os.path.isfile(dest) and (not ours or sha256(dest) != ours):
            return None  # the user's own REFramework: leave it alone
        rel = http_json("https://api.github.com/repos/%s/releases?per_page=1" % REF_REPO)[0]
        tag = rel["tag_name"]
        if state.get("reframework") == tag and os.path.isfile(dest):
            return tag
        url = next((a["browser_download_url"] for a in rel["assets"] if a["name"] == "REFramework.zip"), None)
        if not url:
            raise GsError("o nightly do REFramework não tem REFramework.zip")
        tmp = os.path.join(self.data, "ref_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            zpath = os.path.join(tmp, "REFramework.zip")
            download(url, zpath)
            with zipfile.ZipFile(zpath) as z:
                if "dinput8.dll" not in z.namelist():
                    raise GsError("REFramework.zip sem dinput8.dll")
                with z.open("dinput8.dll") as src, open(dest + ".new", "wb") as out:
                    shutil.copyfileobj(src, out)
            os.replace(dest + ".new", dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        state["reframework_sha"] = sha256(dest)
        cfg = os.path.join(g["exe_dir"], "re2_fw_config.txt")
        if not os.path.exists(cfg):
            with open(cfg, "w") as f:
                f.write("REFrameworkConfig_MenuKey_V2=%d\n" % VK_DELETE)
        else:
            with open(cfg, "r", encoding="latin-1") as f:
                text = f.read()
            if re.search(r"^REFrameworkConfig_MenuKey_V2=45\s*$", text, re.M):
                text = re.sub(r"^REFrameworkConfig_MenuKey_V2=45\s*$", "REFrameworkConfig_MenuKey_V2=%d" % VK_DELETE, text, flags=re.M)
                with open(cfg, "w", encoding="latin-1") as f:
                    f.write(text)
        return tag

    def _adopt_legacy(self, opti, backup):
        """Carries the originals of an ANR "full" install over to our record. Returns (proxy, originals)."""
        rec = read_json(os.path.join(opti, "nr_fg_install.json"), {}) or {}
        proxy = rec.get("proxy") or "dxgi.dll"
        originals = rec.get("originals") or {}
        old = os.path.join(opti, "nr_fg_backup")
        for name, digest in originals.items():
            src = os.path.join(old, name)
            if not os.path.isfile(src) or sha256(src) != digest:
                raise GsError("o backup de %s da instalação anterior não confere; nada foi alterado" % name)
        for name in originals:
            shutil.copy2(os.path.join(old, name), os.path.join(backup, name))
        return proxy, originals

    def install(self, appid, override=None):
        g = self.game(appid)
        # What the game has now (our record, or the ini of an install made by hand) is the base; the caller's choices go on top.
        opts = self.options_for(appid, dict(g["options"], **(override or {})))
        st = self.state()
        if not self.ready(st):
            raise GsError("os pacotes ainda não foram baixados; use 'Buscar atualização'")
        src = self._opti_tree(st["opti"])
        addon = os.path.join(self._dir("addon", st["anr"]), ANR_FOLDER)
        game = g["exe_dir"]
        busy = self._running(game)
        if busy:
            raise GsError("feche o jogo primeiro (%s está em execução)" % busy)
        if g["foreign"]:
            raise GsError("este jogo já tem um OptiScaler que não foi instalado pelo plugin; remova-o antes")
        opti = os.path.join(game, "OptiScaler")
        backup = os.path.join(opti, "greenscaler_backup")
        meta_path = os.path.join(opti, "greenscaler.json")
        ini_path = os.path.join(game, "OptiScaler.ini")
        meta = read_json(meta_path, {}) or {}
        fresh = not g["installed"]
        os.makedirs(backup, exist_ok=True)
        profile = g.get("profile") or {}
        want_proxy = opts.get("proxy", "auto")
        prev_env = list(meta.get("applied_env") or [])
        prev_args = list(meta.get("applied_args") or [])

        if g["managed"]:
            old_proxy = meta.get("proxy") or "dxgi.dll"
            proxy = want_proxy if want_proxy in PROXIES else old_proxy
            if proxy != old_proxy:
                # Safely swap proxies: restore or remove old proxy, then back up any original at new proxy path
                originals = meta.get("originals") or {}
                old_dst = os.path.join(game, old_proxy)
                if old_proxy in originals:
                    if sha256(os.path.join(backup, old_proxy)) != originals[old_proxy]:
                        raise GsError("o backup de %s não confere; troca de proxy cancelada" % old_proxy)
                    copy_over(os.path.join(backup, old_proxy), old_dst)
                    originals.pop(old_proxy, None)
                elif os.path.lexists(old_dst):
                    os.remove(old_dst)
                new_dst = os.path.join(game, proxy)
                if os.path.exists(new_dst) and proxy not in originals:
                    shutil.copy2(new_dst, os.path.join(backup, proxy))
                    if sha256(os.path.join(backup, proxy)) != sha256(new_dst):
                        raise GsError("o backup de %s não confere" % proxy)
                    originals[proxy] = sha256(new_dst)
                meta["originals"] = originals
        elif g["legacy"]:
            proxy, originals = self._adopt_legacy(opti, backup)
            meta.update({"originals": originals})
        else:
            if want_proxy in PROXIES:
                proxy = want_proxy
            elif profile.get("proxy") in PROXIES:
                proxy = profile["proxy"]
            else:
                proxy = self._detect_proxy(os.path.join(game, g["exe"]))
            originals = {}
            for name in (proxy, "OptiScaler.ini"):
                path = os.path.join(game, name)
                if os.path.exists(path):
                    shutil.copy2(path, os.path.join(backup, name))
                    if sha256(os.path.join(backup, name)) != sha256(path):
                        raise GsError("o backup de %s não confere" % name)
                    originals[name] = sha256(path)
            meta = {"originals": originals}
        if proxy not in PROXIES:
            raise GsError("proxy inválido no registro: %s" % proxy)
        # Written before anything is copied, so an install cut short can still be removed cleanly.
        meta.update({"layout": LAYOUT, "proxy": proxy, "root_files": meta.get("root_files", [])})
        write_json(meta_path, meta)
        if g["legacy"]:
            os.remove(os.path.join(opti, "nr_fg_install.json"))
            shutil.rmtree(os.path.join(opti, "nr_fg_backup"), ignore_errors=True)

        # An update keeps the ini already in the game (it may carry the user's own tweaks).
        kept_ini = None
        if not fresh and os.path.exists(ini_path):
            with open(ini_path, "rb") as f:
                kept_ini = f.read()
            shutil.copy2(ini_path, os.path.join(backup, "OptiScaler.ini.before-update"))
        dest = os.path.join(opti, "custom", ANR_FOLDER)
        old = os.path.join(opti, "custom", "nrfg")
        if os.path.isdir(old):
            if os.path.exists(dest):
                shutil.rmtree(old)
            else:
                os.rename(old, dest)
        cfg = os.path.join(dest, "dlssg", "nr_fg.ini")
        kept_cfg = None
        if os.path.exists(cfg):
            with open(cfg, "rb") as f:
                kept_cfg = f.read()

        warn = []
        for item in sorted(os.listdir(src)):
            path = os.path.join(src, item)
            if item in OPTI_SKIP:
                continue
            if item == "OptiScaler.dll":
                copy_over(path, os.path.join(game, proxy))
            elif item == "OptiScaler.ini":
                if kept_ini is None:
                    copy_over(path, ini_path)
            elif item == "OptiScaler":
                copy_over(path, opti)
            elif item == "Licenses":
                copy_over(path, os.path.join(opti, "Licenses"))
            else:
                # Something new in the archive's root: never write over a file of the game.
                target = os.path.join(game, item)
                if os.path.lexists(target) and item not in meta["root_files"]:
                    warn.append("arquivo novo do OptiScaler ignorado: %s" % item)
                    continue
                copy_over(path, target)
                if item not in meta["root_files"]:
                    meta["root_files"].append(item)

        with open(ini_path, "rb") as f:
            base = (kept_ini if kept_ini is not None else f.read()).decode("latin-1")
        copy_over(addon, dest)
        if kept_cfg is not None:
            with open(cfg, "wb") as f:
                f.write(kept_cfg)
        record = os.path.join(dest, "nr_fg_install.json")
        if not os.path.exists(record):  # the shape ANR's add-on uninstall.sh reads
            before = {k: ini_get(base, "FrameGen", k) or "" for k in ("FGInput", "FGNvngxReplacement", "DisableHudless")}
            with open(record, "w") as f:
                f.write(json.dumps({"optiscaler_ini": ini_path, "before": before}) + "\n")
        extra_ini = profile.get("ini") if opts.get("profile_tweaks", True) else None
        extra_env = dict(profile.get("env") or {}) if opts.get("profile_tweaks", True) else {}
        extra_args = list(profile.get("launch_args") or []) if opts.get("profile_tweaks", True) else []
        with open(ini_path, "wb") as f:
            f.write(apply_options(base, opts, extra_ini=extra_ini).encode("latin-1"))

        sl_tag = None
        sl_dir = os.path.join(opti, "streamline")
        if opts.get("streamline"):
            try:
                sl_tag = self._streamline(opti, meta)
            except (GsError, OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile) as e:
                warn.append("o Streamline SDK falhou: %s" % e)
        else:
            if os.path.isdir(sl_dir):
                shutil.rmtree(sl_dir, ignore_errors=True)
            meta.pop("streamline", None)

        mfg_tag = None
        mfg_asi = os.path.join(opti, "plugins", "MFGUnlocker.asi")
        if opts.get("mfg_unlocker"):
            try:
                mfg_tag = self._mfg_unlocker(opti, meta)
            except (GsError, OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile) as e:
                warn.append("o MFG-Unlocker falhou: %s" % e)
        else:
            if os.path.lexists(mfg_asi):
                os.remove(mfg_asi)
            meta.pop("mfg_unlocker", None)

        ref_tag = None
        if g["re_engine"] and opts.get("reframework"):
            try:
                ref_tag = self._reframework(g, meta)
            except (GsError, OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile) as e:
                warn.append("o REFramework falhou: %s" % e)
        if ref_tag:
            meta["reframework"] = ref_tag
        dinput8 = bool(meta.get("reframework_sha"))
        meta.pop("version", None)
        meta.update({"opti": st["opti"], "anr": st["anr"], "options": {k: opts[k] for k in DEFAULTS},
                     "applied_env": list(extra_env.keys()), "applied_args": extra_args,
                     "installed_at": int(time.time())})
        write_json(meta_path, meta)
        self._remember(appid, opts)
        self.log("installed OptiScaler %s + ANR %s in %s as %s" % (st["opti"], st["anr"], game, proxy))
        return {"ok": True, "appid": g["appid"], "opti": st["opti"], "anr": st["anr"], "proxy": proxy,
                "dinput8": dinput8, "fsr4": bool(opts.get("fsr4")), "reframework": ref_tag,
                "streamline": sl_tag, "mfg_unlocker": mfg_tag,
                "extra_env": extra_env, "extra_args": extra_args,
                "prev_env": prev_env, "prev_args": prev_args,
                "warning": "; ".join(warn) or None}

    def uninstall(self, appid):
        g = self.game(appid)
        game = g["exe_dir"]
        opti = os.path.join(game, "OptiScaler")
        if not g["installed"]:
            raise GsError("não há instalação nossa neste jogo")
        busy = self._running(game)
        if busy:
            raise GsError("feche o jogo primeiro (%s está em execução)" % busy)
        meta = read_json(os.path.join(opti, "greenscaler.json"), {}) or {}
        if g["managed"]:
            proxy, originals, backup = meta.get("proxy") or "dxgi.dll", meta.get("originals") or {}, os.path.join(opti, "greenscaler_backup")
        else:
            rec = read_json(os.path.join(opti, "nr_fg_install.json"), {}) or {}
            proxy, originals, backup = rec.get("proxy") or "dxgi.dll", rec.get("originals") or {}, os.path.join(opti, "nr_fg_backup")
        if proxy not in PROXIES:
            raise GsError("proxy inválido no registro: %s" % proxy)
        for name in (proxy, "OptiScaler.ini"):
            if name in originals and sha256(os.path.join(backup, name)) != originals[name]:
                raise GsError("o backup de %s não confere com o original; nada foi alterado" % name)
        for name in (proxy, "OptiScaler.ini"):
            dst = os.path.join(game, name)
            if name in originals:
                copy_over(os.path.join(backup, name), dst)
            elif os.path.lexists(dst):
                os.remove(dst)
        for name in meta.get("root_files", []):
            path = os.path.join(game, os.path.basename(name))
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.remove(path)
        dinput8 = False
        ref = os.path.join(game, "dinput8.dll")
        if meta.get("reframework_sha") and os.path.isfile(ref) and sha256(ref) == meta["reframework_sha"]:
            os.remove(ref)
            dinput8 = True
        log = os.path.join(game, "OptiScaler.log")
        if os.path.exists(log):
            os.remove(log)
        prev_env = list(meta.get("applied_env") or [])
        prev_args = list(meta.get("applied_args") or [])
        shutil.rmtree(opti)
        self.log("uninstalled from %s" % game)
        return {"ok": True, "appid": g["appid"], "dinput8": dinput8,
                "prev_env": prev_env, "prev_args": prev_args}

    def apply_updates(self):
        """Reinstalls the current versions in every managed game that has other ones. Skips running games."""
        done, skipped = [], []
        if not self.ready():
            return {"updated": done, "skipped": skipped}
        for g in self.games():
            if g["outdated"]:
                try:
                    self.install(g["appid"])
                    done.append(g["name"])
                except (GsError, OSError) as e:
                    skipped.append("%s: %s" % (g["name"], e))
        return {"updated": done, "skipped": skipped}


if __name__ == "__main__":
    import sys
    core = Core(os.path.expanduser("~"), os.environ.get("GS_DATA", os.path.expanduser("~/.cache/greenscaler-dev")), print)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for game in core.games():
            print(json.dumps(game, ensure_ascii=False))
    elif cmd == "check":
        print(core.check_update(force=True))
    elif cmd == "updates":
        print(core.apply_updates())
    elif cmd == "install":
        print(core.install(sys.argv[2]))
    elif cmd == "uninstall":
        print(core.uninstall(sys.argv[2]))
