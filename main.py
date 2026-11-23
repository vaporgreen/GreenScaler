import asyncio
import os

import decky
from greenscaler import Core, GsError, merge_launch, strip_launch


class Plugin:
    async def _main(self):
        self.core = Core(decky.DECKY_USER_HOME, decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger.info)
        self.loop = asyncio.get_event_loop()
        self.loop.create_task(self._background_update())

    async def _unload(self):
        pass

    async def _uninstall(self):
        # Games keep their install: removing the plugin must not change game folders behind the user's back.
        pass

    async def _run(self, fn, *args):
        try:
            return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
        except GsError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # reported to the UI instead of dying silently in the backend
            decky.logger.exception("unexpected error")
            return {"ok": False, "error": "erro inesperado: %s" % e}

    async def _background_update(self):
        await asyncio.sleep(20)
        s = self.core.settings()
        if not s["auto_update"]:
            return
        res = await self._run(self.core.check_update, False)
        if res.get("changed") and s["auto_apply"]:
            applied = await self._run(self.core.apply_updates)
            if applied.get("updated"):
                await decky.emit("gs_updated", "%s + ANR %s" % (res.get("opti"), res.get("anr")), applied["updated"])

    def _state(self):
        st = self.core.state()
        return {"ok": True, "games": self.core.games(), "settings": self.core.settings(),
                "opti": st["opti"], "anr": st["anr"], "prev": st["prev"], "pinned": st["pinned"],
                "checked_at": st["checked_at"], "ready": self.core.ready(st)}

    async def get_state(self):
        return await self._run(self._state)

    async def _apply(self, res, always=False):
        if res.get("ok") is False:
            return res
        applied = {"updated": [], "skipped": []}
        if always or self.core.settings()["auto_apply"]:
            applied = await self._run(self.core.apply_updates)
        return {"ok": True, "opti": res.get("opti"), "anr": res.get("anr"), "changed": res.get("changed", False),
                "updated": applied.get("updated", []), "skipped": applied.get("skipped", [])}

    async def check_update(self):
        return await self._apply(await self._run(self.core.check_update, True))

    async def rollback(self):
        return await self._apply(await self._run(self.core.rollback), True)

    async def save_settings(self, patch):
        return await self._run(self.core.save_settings, patch)

    async def install(self, appid, options, launch_options):
        res = await self._run(self.core.install, appid, options)
        if res.get("ok"):
            res["launch_options"] = merge_launch(launch_options, res["proxy"], res["dinput8"], res["fsr4"])
        return res

    async def uninstall(self, appid, launch_options):
        res = await self._run(self.core.uninstall, appid)
        if res.get("ok"):
            res["launch_options"] = strip_launch(launch_options, res["dinput8"])
        return res
