#!/usr/bin/env python3
"""Unit tests for GreenScaler v0.5.0 profiles, Proton inspection, disk scanner, and launch merging."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

from greenscaler.core import Core, apply_options, merge_launch, strip_launch
from greenscaler.profiles import (
    ProfileDB,
    classify_proton,
    list_custom_protons,
    normalize_title,
    read_proton_mapping,
)


def test_normalize_and_lookup():
    assert normalize_title("Dragon's Dogma 2") == "dragons dogma 2"
    assert normalize_title("The Blood of Dawnwalker™") == "the blood of dawnwalker"
    with tempfile.TemporaryDirectory() as tmp:
        db = ProfileDB(tmp)
        meta = db.meta()
        assert meta["count"] >= 700, f"expected >=700 wiki games, got {meta['count']}"
        assert meta["curated"] >= 20

        # Exact curated + wiki merge
        p = db.lookup("3751260", "The Blood of Dawnwalker")
        assert p is not None
        assert p["source"] == "wiki+curated"
        assert p["streamline"] is True
        assert p["env"]["PROTON_VKD3D_LOWLATENCY"] == "1"
        assert p["proxy"] == "dxgi.dll"

        # Pure wiki lookup by normalized title (non-curated)
        p2 = db.lookup("9999999", "A Quiet Place: The Road Ahead")
        assert p2 is not None
        assert p2["source"] == "wiki"
        assert p2["proxy"] == "winmm.dll"
        assert p2["linux_tested"] is True

        # Curated AppID + wiki alias
        p3 = db.lookup("2054970", "Dragon's Dogma 2")
        assert p3 is not None
        assert p3["reframework"] is True
        assert p3["ini"]["Spoofing"]["Dxgi"] == "false"


def test_proton_inspector():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_dir = os.path.join(tmp, "config")
        compat_dir = os.path.join(tmp, "compatibilitytools.d")
        os.makedirs(cfg_dir)
        os.makedirs(os.path.join(compat_dir, "Proton-CachyOS Latest"))
        os.makedirs(os.path.join(compat_dir, "Proton-GE Latest"))
        open(os.path.join(compat_dir, "Proton-CachyOS Latest", "proton"), "w").close()
        open(os.path.join(compat_dir, "Proton-GE Latest", "proton"), "w").close()
        with open(os.path.join(cfg_dir, "config.vdf"), "w") as f:
            f.write('''"InstallConfigStore"
{
  "Software"
  {
    "Valve"
    {
      "Steam"
      {
        "CompatToolMapping"
        {
          "3751260" { "name" "proton_experimental" "config" "" "priority" "250" }
          "4115450" { "name" "proton-stable" "config" "" "priority" "250" }
          "3357650" { "name" "Proton-CachyOS Latest" "config" "" "priority" "250" }
        }
      }
    }
  }
}
''')
        custom = list_custom_protons([tmp])
        assert "Proton-CachyOS Latest" in custom
        assert "Proton-GE Latest" in custom
        pmap, pdef = read_proton_mapping([tmp])
        assert pmap["3751260"] == "proton_experimental"
        assert pmap["4115450"] == "proton-stable"

        c_ok = classify_proton("Proton-CachyOS Latest", custom)
        assert c_ok["fsr4_ready"] is True and c_ok["level"] == "ok"

        c_warn = classify_proton("proton-stable", custom)
        assert c_warn["fsr4_ready"] is False and c_warn["level"] == "warn"
        assert "Proton-CachyOS Latest" in c_warn["hint"]


def test_launch_and_ini_overrides():
    merged = merge_launch(
        'MANGOHUD=1 %command% -windowed',
        proxy="winmm.dll",
        dinput8=False,
        fsr4=True,
        extra_env={"PROTON_VKD3D_LOWLATENCY": "1"},
        extra_args=["-dx12"],
    )
    assert 'WINEDLLOVERRIDES="winmm=n,b"' in merged
    assert "PROTON_ENABLE_NVAPI=1" in merged
    assert "PROTON_FSR4_UPGRADE=1" in merged
    assert "PROTON_VKD3D_LOWLATENCY=1" in merged
    assert "MANGOHUD=1" in merged
    assert "-windowed" in merged and "-dx12" in merged

    stripped = strip_launch(merged, dinput8=False, prev_env=["PROTON_VKD3D_LOWLATENCY"], prev_args=["-dx12"])
    assert "PROTON_VKD3D_LOWLATENCY" not in stripped
    assert "-dx12" not in stripped
    assert "MANGOHUD=1" in stripped
    assert "-windowed" in stripped

    ini = "[Upscalers]\nDx12Upscaler=auto\n[Spoofing]\nDxgi=true\n"
    out = apply_options(ini, {"fsr4": True, "fsr4_model": "3", "fg": "1", "profile_tweaks": True},
                        extra_ini={"Spoofing": {"Dxgi": "false"}, "InitFlags": {"UseNtDllHooks": "false"}})
    assert "Dxgi=false" in out
    assert "UseNtDllHooks=false" in out
    assert "Fsr4Model=3" in out


def test_disk_scan_markers():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "Game.exe"), "wb").write(b"MZ" + b"\x00" * 100)
        open(os.path.join(tmp, "nvngx_dlss.dll"), "wb").write(b"")
        open(os.path.join(tmp, "sl.interposer.dll"), "wb").write(b"")
        open(os.path.join(tmp, "sl.dlss_g.dll"), "wb").write(b"")
        open(os.path.join(tmp, "amd_fidelityfx_dx12.dll"), "wb").write(b"")
        open(os.path.join(tmp, "libxess.dll"), "wb").write(b"")
        exes, marks = Core._scan(tmp)
        assert len(exes) == 1
        assert marks["dlss"] and marks["dlssg"] and marks["streamline"] and marks["fsr"] and marks["xess"]
        assert not marks["anticheat"]


if __name__ == "__main__":
    test_normalize_and_lookup()
    test_proton_inspector()
    test_launch_and_ini_overrides()
    test_disk_scan_markers()
    print("ALL GREENSCALER v0.5.0 TESTS PASSED!")
