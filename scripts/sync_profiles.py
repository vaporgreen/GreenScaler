#!/usr/bin/env python3
"""Builds py_modules/greenscaler/data/profiles.json from optiscaler/OptiScaler.wiki.git
and scripts/curated_overrides.json.

Pure Python 3 standard library — safe to run locally or in GitHub Actions.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

WIKI_URL = "https://github.com/optiscaler/OptiScaler.wiki.git"
VALID_PROXIES = ("dxgi.dll", "winmm.dll", "version.dll")


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


def strip_markup(text):
    if not text:
        return ""
    # Remove HTML strikethrough / tags
    text = re.sub(r"\+\+\+<s>.*?</s>\+\+\+", "", text, flags=re.S)
    text = re.sub(r"<s>.*?</s>", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # Markdown links [label](url) -> label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # AsciiDoc links https://...[label] -> label
    text = re.sub(r"https?://\S+?\[([^\]]*)\]", lambda m: m.group(1) or "", text)
    text = re.sub(r"https?://\S+", "", text)
    # Emphasis markers
    text = re.sub(r"[*_`~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ;.-•")
    return text


def split_inputs(raw):
    clean = strip_markup(raw)
    if not clean or clean == "-":
        return []
    parts = re.split(r"[,;+]|\band\b", clean)
    out = []
    for p in parts:
        p = p.strip()
        if p and p != "-" and len(p) <= 32 and p not in out:
            out.append(p)
    return out


def parse_asciidoc(path):
    """Parses OptiScaler's standard [cols='1,1'] AsciiDoc compatibility entry."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return {}

    # Split into table cells starting with |**Header**
    fields = {}
    matches = list(re.finditer(r"(?m)^\|\s*\*\*([^*]+?)\*\*\s*$", raw))
    for i, m in enumerate(matches):
        key = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else raw.find("|===", start)
        if end == -1:
            end = len(raw)
        val = raw[start:end].strip()
        # Drop leading AsciiDoc cell prefix `a|` or `|`
        val = re.sub(r"^(?:a)?\|\s*", "", val)
        fields[key] = val.strip()

    info = {}

    # Filename (proxy DLL)
    fn_raw = fields.get("filename", "")
    fn_low = fn_raw.lower()
    # Ignore Xbox-only / SpecialK-only hints when picking default proxy
    found_proxies = [p for p in VALID_PROXIES if p in fn_low]
    if found_proxies:
        # Prefer dxgi.dll if explicitly listed first alongside alternatives, else first valid proxy
        first_pos = min((fn_low.find(p), p) for p in found_proxies)[1]
        info["proxy"] = first_pos

    # OS (Linux tested?)
    os_low = fields.get("os", "").lower()
    if any(k in os_low for k in ("linux", "steamos", "proton", "bazzite", "cachy", "arch", "ubuntu", "fedora")):
        info["linux_tested"] = True

    # Upscaler inputs
    up_raw = fields.get("upscaler inputs", "")
    if up_raw:
        info["inputs"] = split_inputs(up_raw)

    # FG inputs
    fg_raw = fields.get("fg inputs", "") or fields.get("fg-settings", "") or fields.get("fg settings", "")
    fg_clean = strip_markup(fg_raw)
    if fg_clean and fg_clean != "-":
        fg_items = []
        for line in fg_raw.splitlines():
            t = strip_markup(line.lstrip("*.- "))
            if t and t != "-" and not t.lower().startswith("for example"):
                fg_items.append(t)
        info["fg_inputs"] = fg_items[:4] if fg_items else [fg_clean[:80]]
        if re.search(r"dlssg\s+via\s+(sl|streamline)", fg_clean, re.I):
            info["streamline"] = True

    # Notes + Known issues
    issues = fields.get("known issues", "")
    notes = fields.get("notes", "")
    combined = (issues + "\n" + notes).strip()
    if combined:
        if re.search(r"\breframework\b", combined, re.I):
            info["reframework"] = True
        if re.search(r"\b(easy\s*anti[- ]?cheat|eac\b|battleye|anti[- ]?cheat|use at your own risk)\b", combined, re.I):
            info["anticheat"] = True
        ini = {}
        if re.search(r"\bdxgi\s*=\s*false\b", combined, re.I):
            ini.setdefault("Spoofing", {})["Dxgi"] = "false"
        if re.search(r"\busentdllhooks\s*=\s*false\b", combined, re.I):
            ini.setdefault("InitFlags", {})["UseNtDllHooks"] = "false"
        if ini:
            info["ini"] = ini
        if re.search(r"(?:^|\s)-dx12(?:\s|$|[.,;])", combined, re.I) and not re.search(r"cannot be forced", combined, re.I):
            info["launch_args"] = ["-dx12"]

        # Extract concise bullet notes
        bullets = []
        for line in (issues + "\n" + notes).splitlines():
            line = line.strip()
            if not line or line == "---" or line.startswith("|"):
                continue
            cleaned = strip_markup(line.lstrip("*.-+ "))
            if not cleaned or cleaned in ("-", "+", "or") or cleaned.lower().startswith(("install method", "download", "extract", "folder structure")):
                continue
            bullets.append(cleaned)
            if len(" | ".join(bullets)) >= 190:
                break
        if bullets:
            summary = " | ".join(bullets)
            info["note"] = (summary[:197] + "...") if len(summary) > 200 else summary

    return info


def parse_compat_table(wiki_dir):
    compat_path = os.path.join(wiki_dir, "Compatibility-List.md")
    with open(compat_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    by_title = {}
    aliases = {}
    stats = {"total_rows": 0, "with_asciidoc": 0, "linux_tested": 0, "with_proxy": 0, "with_streamline": 0}

    row_re = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|?\s*$")

    for line in lines:
        line = line.rstrip("\r\n")
        if not line.startswith("|"):
            continue
        m = row_re.match(line)
        if not m:
            continue
        col_game, col_compat, col_inputs, col_patcher, col_notes, col_images = m.groups()
        # Skip table header/separator rows
        if col_game.lower().startswith("game") or set(col_game.replace("-", "").replace(":", "").strip()) == set():
            continue
        if not any(sym in col_compat for sym in ("✅", "❌", "➖")):
            continue

        stats["total_rows"] += 1

        # Extract title & optional wiki slug from [Title](Slug)
        slug = None
        lm = re.match(r"^\[([^\]]+)\]\(([^)]+)\)", col_game.strip())
        if lm:
            title = strip_markup(lm.group(1))
            slug = lm.group(2).strip()
        else:
            title = strip_markup(col_game)

        norm = normalize_title(title)
        if not norm:
            continue

        status = "ok" if "✅" in col_compat else ("broken" if "❌" in col_compat else "partial")
        entry = {
            "title": title,
            "status": status,
        }

        inputs = split_inputs(col_inputs)
        if inputs:
            entry["inputs"] = inputs
        if "✨" in col_patcher:
            entry["optipatcher"] = True
        if "🐧" in col_images or "linux" in col_notes.lower():
            entry["linux_tested"] = True

        # Parse inline notes from table row
        note_clean = strip_markup(col_notes)
        if note_clean:
            entry["note"] = (note_clean[:197] + "...") if len(note_clean) > 200 else note_clean
            # Check explicit non-Xbox proxy recommendation in row notes
            if not re.search(r"\bxbox\b", note_clean, re.I):
                pm = re.search(r"\b(?:as|use)\s+(?:optiscaler\s+as\s+)?(winmm\.dll|version\.dll|dxgi\.dll)\b", note_clean, re.I)
                if pm:
                    entry["proxy"] = pm.group(1).lower()
            if re.search(r"\b(eac|easy\s*anti[- ]?cheat|battleye|anti[- ]?cheat|use at your own risk)\b", note_clean, re.I) and not re.search(r"no\s+anti[- ]?cheat", note_clean, re.I):
                entry["anticheat"] = True
            if re.search(r"(?:^|\s)-dx12(?:\s|$|[.,;])", note_clean, re.I) and not re.search(r"cannot be forced", note_clean, re.I):
                entry["launch_args"] = ["-dx12"]
            if re.search(r"\bdisable\s+spoofing\b|\bdxgi\s*=\s*false\b", note_clean, re.I):
                entry.setdefault("ini", {}).setdefault("Spoofing", {})["Dxgi"] = "false"
            if re.search(r"\bhudfix\s*=\s*true\b", note_clean, re.I):
                entry.setdefault("ini", {}).setdefault("OptiFG", {})["HUDFix"] = "true"

        # Enrich with linked .asciidoc page if present
        if slug and not slug.startswith("http"):
            entry["wiki_page"] = slug
            ad_path = os.path.join(wiki_dir, slug + ".asciidoc")
            if os.path.isfile(ad_path):
                stats["with_asciidoc"] += 1
                ad = parse_asciidoc(ad_path)
                for k in ("proxy", "linux_tested", "streamline", "reframework", "anticheat", "fg_inputs", "launch_args"):
                    if ad.get(k):
                        entry[k] = ad[k]
                if ad.get("inputs") and len(ad["inputs"]) > len(entry.get("inputs", [])):
                    entry["inputs"] = ad["inputs"]
                if ad.get("ini"):
                    for sec, kv in ad["ini"].items():
                        entry.setdefault("ini", {}).setdefault(sec, {}).update(kv)
                if ad.get("note") and (not entry.get("note") or "check compat entry" in entry["note"].lower() or len(entry["note"]) < 40):
                    entry["note"] = ad["note"]
                # Also index by normalized slug if different from display title
                norm_slug = normalize_title(slug.replace("-", " "))
                if norm_slug and norm_slug != norm:
                    aliases[norm_slug] = norm

        if entry.get("linux_tested"):
            stats["linux_tested"] += 1
        if entry.get("proxy"):
            stats["with_proxy"] += 1
        if entry.get("streamline"):
            stats["with_streamline"] += 1

        by_title[norm] = entry

    return by_title, aliases, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki-dir", help="Path to existing optiscaler/OptiScaler.wiki checkout")
    ap.add_argument("--overrides", default=os.path.join(os.path.dirname(__file__), "curated_overrides.json"))
    ap.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "..", "py_modules", "greenscaler", "data", "profiles.json"))
    args = ap.parse_args()

    tmp_dir = None
    wiki_dir = args.wiki_dir
    if not wiki_dir:
        tmp_dir = tempfile.mkdtemp(prefix="opti-wiki-")
        subprocess.run(["git", "clone", "--depth", "1", WIKI_URL, tmp_dir], check=True)
        wiki_dir = tmp_dir

    try:
        commit = subprocess.run(
            ["git", "-C", wiki_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False
        ).stdout.strip() or "unknown"

        by_title, aliases, stats = parse_compat_table(wiki_dir)

        overrides = {}
        if os.path.isfile(args.overrides):
            with open(args.overrides, "r", encoding="utf-8") as f:
                overrides = json.load(f)

        out = {
            "schema": 1,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wiki_commit": commit,
            "count": len(by_title),
            "by_appid": overrides.get("by_appid", {}),
            "aliases": aliases,
            "by_title": by_title,
        }

        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

        size_kb = os.path.getsize(args.output) / 1024.0
        print(f"Generated {args.output} ({size_kb:.1f} KB) | wiki={commit} | rows={stats['total_rows']} | asciidoc={stats['with_asciidoc']} | linux={stats['linux_tested']} | proxy={stats['with_proxy']} | streamline={stats['with_streamline']} | curated_appids={len(out['by_appid'])}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
