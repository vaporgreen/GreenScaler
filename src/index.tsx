import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Router,
  Spinner,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaLeaf } from "react-icons/fa";

interface Options {
  fsr4: boolean;
  fsr4_model: string;
  fg: string;
  proxy: string;
  streamline: boolean;
  mfg_unlocker: boolean;
  reframework: boolean;
  profile_tweaks: boolean;
}

interface ProtonInfo {
  tool: string | null;
  label: string;
  fsr4_ready: boolean;
  level: string;
  hint: string;
}

interface GameProfile {
  title?: string;
  status?: string;
  source?: string;
  inputs?: string[];
  fg_inputs?: string[];
  proxy?: string;
  streamline?: boolean;
  optipatcher?: boolean;
  linux_tested?: boolean;
  anticheat?: boolean;
  ini?: Record<string, Record<string, string>>;
  env?: Record<string, string>;
  launch_args?: string[];
  note?: string;
  wiki_page?: string;
}

interface ProtonDBInfo {
  ok: boolean;
  tier: string | null;
  trending?: string | null;
  best?: string | null;
  total?: number;
}

interface Game {
  appid: string;
  name: string;
  installed: boolean;
  managed: boolean;
  legacy: boolean;
  foreign: boolean;
  outdated: boolean;
  opti: string | null;
  anr: string | null;
  proxy: string | null;
  has_dlss: boolean;
  has_dlssg: boolean;
  has_streamline: boolean;
  has_fsr: boolean;
  has_xess: boolean;
  anticheat: boolean;
  re_engine: boolean;
  has_reframework: boolean;
  proton: ProtonInfo;
  profile: GameProfile | null;
  recommended: Options;
  options: Options;
}

interface ProfilesMeta {
  count: number;
  curated: number;
  wiki_commit: string;
  generated_at: string;
}

interface State {
  ok: boolean;
  error?: string;
  games: Game[];
  settings: { auto_update: boolean; auto_apply: boolean; defaults: Options };
  opti: string | null;
  anr: string | null;
  prev: { opti: string | null; anr: string | null } | null;
  pinned: boolean;
  ready: boolean;
  profiles?: ProfilesMeta;
}

interface Result {
  ok: boolean;
  error?: string;
  warning?: string | null;
  launch_options?: string;
  opti?: string;
  anr?: string;
  changed?: boolean;
  updated?: string[];
  skipped?: string[];
}

const getState = callable<[], State>("get_state");
const getProtonDB = callable<[appid: string], ProtonDBInfo>("get_protondb");
const checkUpdate = callable<[], Result>("check_update");
const rollback = callable<[], Result>("rollback");
const saveSettings = callable<[patch: Record<string, unknown>], unknown>("save_settings");
const install = callable<[appid: string, options: Options, launch: string], Result>("install");
const uninstall = callable<[appid: string, launch: string], Result>("uninstall");

const FG_CHOICES = [
  { data: "off", label: "Desligada" },
  { data: "game", label: "Seguir o menu do jogo" },
  { data: "1", label: "2X" },
  { data: "2", label: "3X" },
  { data: "3", label: "4X" },
  { data: "4", label: "5X" },
  { data: "5", label: "6X" },
];

const MODEL_CHOICES = [
  { data: "auto", label: "Automático" },
  { data: "0", label: "FP8 (RDNA 4)" },
  { data: "1", label: "INT8" },
  { data: "2", label: "INT8 RDNA 2" },
  { data: "3", label: "INT8 Lite (portáteis)" },
];

const PROXY_CHOICES = [
  { data: "auto", label: "Automático (Wiki / Engine)" },
  { data: "dxgi.dll", label: "dxgi.dll (DX12 padrão)" },
  { data: "winmm.dll", label: "winmm.dll (Vulkan / alternativo)" },
  { data: "version.dll", label: "version.dll (alternativo)" },
];

const PROTONDB_LABELS: Record<string, string> = {
  platinum: "Platinum",
  gold: "Gold",
  silver: "Silver",
  bronze: "Bronze",
  borked: "Borked",
  native: "Native",
  pending: "Pendente",
};

// Steam only hands out an app's launch options through the details callback.
function currentLaunchOptions(appid: number): Promise<string> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value: string) => {
      if (done) return;
      done = true;
      resolve(value);
    };
    try {
      const reg = (window as any).SteamClient.Apps.RegisterForAppDetails(appid, (details: any) => {
        finish(details?.strLaunchOptions ?? "");
        setTimeout(() => reg?.unregister?.(), 0);
      });
      setTimeout(() => {
        reg?.unregister?.();
        finish("");
      }, 2500);
    } catch {
      finish("");
    }
  });
}

function setLaunchOptions(appid: number, value: string) {
  (window as any).SteamClient.Apps.SetAppLaunchOptions(appid, value);
}

function notify(title: string, body: string) {
  toaster.toast({ title, body });
}

// The quick access menu unmounts this panel whenever a dropdown or another view takes over,
// so what the user picked lives here, outside the component, and survives the remount.
// The app the user is looking at: the library page open behind the menu, else the running game.
function contextApp(): string | null {
  try {
    const path: string =
      (Router as any).WindowStore?.GamepadUIMainWindowInstance?.m_history?.location?.pathname ?? "";
    const page = path.match(/\/library\/app\/(\d+)/);
    if (page) return page[1];
  } catch {
    // Steam's internals moved: fall through to the running game
  }
  try {
    const running = (Router as any).MainRunningApp?.appid;
    if (running) return String(running);
  } catch {
    // no running app information
  }
  return null;
}

const ui: {
  selected: string | null;
  context: string | null | undefined;
  edits: Record<string, Options>;
  protondb: Record<string, ProtonDBInfo>;
  state: State | null;
} = {
  context: undefined,
  selected: null,
  edits: {},
  protondb: {},
  state: null,
};

function summarizeDetected(g: Game): string {
  const tags: string[] = [];
  if (g.has_dlss) tags.push("DLSS");
  if (g.has_dlssg) tags.push("DLSS-G");
  if (g.has_streamline) tags.push("Streamline");
  if (g.has_fsr) tags.push("FSR");
  if (g.has_xess) tags.push("XeSS");
  if (g.re_engine) tags.push("RE Engine");
  return tags.length ? tags.join(" · ") : "Nenhum upscaler detectado no disco";
}

function summarizeProfileTweaks(p: GameProfile | null): string {
  if (!p) return "";
  const parts: string[] = [];
  if (p.proxy) parts.push("proxy " + p.proxy);
  if (p.streamline) parts.push("Streamline");
  if (p.env) {
    for (const [k, v] of Object.entries(p.env)) parts.push(`${k}=${v}`);
  }
  if (p.launch_args?.length) parts.push(p.launch_args.join(" "));
  if (p.ini) {
    for (const [sec, kv] of Object.entries(p.ini)) {
      for (const [k, v] of Object.entries(kv)) parts.push(`[${sec}] ${k}=${v}`);
    }
  }
  return parts.join(" · ");
}

function hasExtraTweaks(p: GameProfile | null): boolean {
  if (!p) return false;
  return Boolean(
    (p.ini && Object.keys(p.ini).length > 0) ||
      (p.env && Object.keys(p.env).length > 0) ||
      (p.launch_args && p.launch_args.length > 0)
  );
}

function Content() {
  const [state, setStateRaw] = useState<State | null>(ui.state);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelectedRaw] = useState<string | null>(ui.selected);
  const [edits, setEditsRaw] = useState<Record<string, Options>>(ui.edits);
  const [pdbMap, setPdbMapRaw] = useState<Record<string, ProtonDBInfo>>(ui.protondb);

  const setState = (s: State) => {
    ui.state = s;
    setStateRaw(s);
  };
  const setSelected = (appid: string) => {
    ui.selected = appid;
    setSelectedRaw(appid);
  };
  const setEdits = (e: Record<string, Options>) => {
    ui.edits = e;
    setEditsRaw(e);
  };
  const setPdb = (appid: string, info: ProtonDBInfo) => {
    ui.protondb = { ...ui.protondb, [appid]: info };
    setPdbMapRaw(ui.protondb);
  };

  const refresh = async () => {
    const s = await getState();
    setState(s);
    if (!s.ok || !s.games.length) return;
    // The game in front of the user wins once, when it changes; after that a manual pick stays.
    const context = contextApp();
    if (context !== ui.context) {
      ui.context = context;
      if (context && s.games.some((g) => g.appid === context)) {
        setSelected(context);
        return;
      }
    }
    if (!s.games.some((g) => g.appid === ui.selected)) {
      setSelected((s.games.find((g) => g.managed) ?? s.games[0]).appid);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!selected || ui.protondb[selected]) return;
    let alive = true;
    getProtonDB(selected)
      .then((info) => {
        if (alive && info) setPdb(selected, info);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selected]);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Spinner style={{ height: 24 }} />
        </PanelSectionRow>
      </PanelSection>
    );
  }
  if (!state.ok) {
    return (
      <PanelSection title="GreenScaler">
        <PanelSectionRow>
          <Field label="Erro" description={state.error} />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const game = state.games.find((g) => g.appid === selected) ?? null;
  const opts: Options | null = game ? edits[game.appid] ?? game.options : null;
  const pdb: ProtonDBInfo | null = game ? pdbMap[game.appid] ?? null : null;
  const setOpt = (patch: Partial<Options>) => {
    if (game && opts) setEdits({ ...edits, [game.appid]: { ...opts, ...patch } });
  };

  const run = async (label: string, work: () => Promise<void>) => {
    setBusy(label);
    try {
      await work();
    } catch (e) {
      notify("GreenScaler", "Falhou: " + String(e));
    } finally {
      setBusy(null);
      await refresh();
    }
  };

  const onCheck = () =>
    run("Buscando versões novas...", async () => {
      const r = await checkUpdate();
      if (!r.ok) return notify("GreenScaler", r.error ?? "Falhou");
      const games = r.updated?.length ? " Atualizado em: " + r.updated.join(", ") + "." : "";
      const skipped = r.skipped?.length ? " Pulados: " + r.skipped.join("; ") + "." : "";
      const pair = r.opti + " + ANR " + r.anr;
      notify("GreenScaler", (r.changed ? "Novas versões: " + pair + "." : "Já está nas mais novas (" + pair + ").") + games + skipped);
    });

  const onRollback = () =>
    run("Voltando à versão anterior...", async () => {
      const r = await rollback();
      if (!r.ok) return notify("GreenScaler", r.error ?? "Falhou");
      notify("GreenScaler", "Voltou para " + r.opti + " + ANR " + r.anr + ". A atualização automática fica pausada até a próxima busca manual.");
    });

  const onLoadRecommended = () => {
    if (!game) return;
    setEdits({ ...edits, [game.appid]: { ...game.recommended } });
    const tweaks = summarizeProfileTweaks(game.profile);
    notify(
      game.name,
      "Perfil recomendado carregado" + (tweaks ? " (" + tweaks + ")" : "") + ". Clique em Instalar/Aplicar para gravar."
    );
  };

  const onInstall = () =>
    run("Instalando...", async () => {
      if (!game || !opts) return;
      const appid = Number(game.appid);
      const r = await install(game.appid, opts, await currentLaunchOptions(appid));
      if (!r.ok) return notify(game.name, r.error ?? "Falhou");
      setLaunchOptions(appid, r.launch_options ?? "");
      const next = { ...edits };
      delete next[game.appid];
      setEdits(next);
      notify(game.name, "OptiScaler " + r.opti + " + ANR " + r.anr + " instalados. Opções de inicialização ajustadas." + (r.warning ? " Aviso: " + r.warning : ""));
    });

  const onUninstall = () =>
    run("Removendo...", async () => {
      if (!game) return;
      const appid = Number(game.appid);
      const r = await uninstall(game.appid, await currentLaunchOptions(appid));
      if (!r.ok) return notify(game.name, r.error ?? "Falhou");
      setLaunchOptions(appid, r.launch_options ?? "");
      notify(game.name, "Removido. Arquivos originais restaurados.");
    });

  const status = (g: Game) => {
    if (g.foreign) return "OptiScaler de outra origem";
    if (!g.installed) return "Não instalado";
    if (g.legacy) return "Instalação antiga ou feita à mão: Aplicar converte para OptiScaler nightly + ANR add-on";
    const px = g.proxy ? " · " + g.proxy : "";
    return "OptiScaler " + g.opti + " + ANR " + g.anr + px + (g.outdated ? " (atualização disponível)" : "");
  };

  const profileHeader = (p: GameProfile) => {
    const badges: string[] = [];
    if (p.status === "broken") badges.push("❌ Incompatível");
    else if (p.status === "partial") badges.push("⚠️ Parcial");
    else badges.push("✅ Compatível");
    if (p.linux_tested) badges.push("🐧 Linux");
    if (p.optipatcher) badges.push("✨ OptiPatcher");
    return badges.join(" · ");
  };

  const protonLine = (g: Game) => {
    const tier = pdb?.ok && pdb.tier ? PROTONDB_LABELS[pdb.tier.toLowerCase()] ?? pdb.tier : null;
    const base = g.proton.label + (tier ? ` · ProtonDB: ${tier}` : "");
    return g.proton.hint ? `${base} — ${g.proton.hint}` : base;
  };

  return (
    <>
      <PanelSection title="Versões">
        <PanelSectionRow>
          <Field label="OptiScaler nightly" description={state.ready ? state.opti : "não baixado"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="ANR FG (add-on)"
            description={(state.ready ? state.anr : "não baixado") + (state.pinned ? " · fixadas pelo retorno de versão" : "")}
          />
        </PanelSectionRow>
        {state.profiles && (
          <PanelSectionRow>
            <Field
              label="Base Wiki OptiScaler"
              description={`${state.profiles.count} jogos (${state.profiles.curated} perfis Proton curados)`}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!!busy} onClick={onCheck}>
            {busy ?? "Buscar atualização"}
          </ButtonItem>
        </PanelSectionRow>
        {state.prev?.opti && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!busy} onClick={onRollback}>
              Voltar para as versões anteriores
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Atualizar sozinho"
            description="Busca o OptiScaler nightly, ANR e a base da Wiki ao iniciar e reaplica nos jogos gerenciados"
            checked={state.settings.auto_update}
            onChange={async (v) => {
              await saveSettings({ auto_update: v, auto_apply: v });
              refresh();
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Jogo">
        {state.games.length === 0 && (
          <PanelSectionRow>
            <Field label="Nenhum jogo encontrado" />
          </PanelSectionRow>
        )}
        {game && opts && (
          <>
            <PanelSectionRow>
              <DropdownItem
                label="Jogo"
                rgOptions={state.games.map((g) => ({ data: g.appid, label: g.name }))}
                selectedOption={game.appid}
                onChange={(o) => setSelected(o.data)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field label="Estado" description={status(game)} />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field label="Detectado no disco" description={summarizeDetected(game)} />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field
                label={game.proton.level === "warn" && opts.fsr4 ? "⚠️ Proton" : "Proton"}
                description={protonLine(game)}
              />
            </PanelSectionRow>
            {game.profile && (
              <>
                <PanelSectionRow>
                  <Field
                    label={"Wiki OptiScaler · " + profileHeader(game.profile)}
                    description={
                      [
                        game.profile.inputs?.length ? "Inputs: " + game.profile.inputs.join(", ") : "",
                        summarizeProfileTweaks(game.profile) ? "Ajustes: " + summarizeProfileTweaks(game.profile) : "",
                        game.profile.note ?? "",
                      ]
                        .filter(Boolean)
                        .join(" — ")
                    }
                  />
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem layout="below" disabled={!!busy} onClick={onLoadRecommended}>
                    Carregar perfil recomendado
                  </ButtonItem>
                </PanelSectionRow>
              </>
            )}
            {!game.has_dlssg && !game.profile?.streamline && (
              <PanelSectionRow>
                <Field
                  label="Sem DLSS Frame Generation"
                  description="O ANR FG entra no lugar do DLSS-G. Neste jogo só os upscalers devem funcionar."
                />
              </PanelSectionRow>
            )}
            {game.anticheat && (
              <PanelSectionRow>
                <Field
                  label="⚠️ Anti-cheat detectado"
                  description="Jogos online com anti-cheat podem banir por arquivos modificados. Use por sua conta e risco."
                />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <ToggleField
                label="FSR 4"
                description="Exige Proton-GE ou Proton-CachyOS"
                checked={opts.fsr4}
                onChange={(v) => setOpt({ fsr4: v })}
              />
            </PanelSectionRow>
            {opts.fsr4 && (
              <PanelSectionRow>
                <DropdownItem
                  label="Modelo do FSR 4"
                  rgOptions={MODEL_CHOICES}
                  selectedOption={opts.fsr4_model}
                  onChange={(o) => setOpt({ fsr4_model: o.data })}
                />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <DropdownItem
                label="Geração de quadros (ANR)"
                description="Em portáteis, acima de 2X o custo de GPU costuma não compensar"
                rgOptions={FG_CHOICES}
                selectedOption={opts.fg}
                onChange={(o) => setOpt({ fg: o.data })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <DropdownItem
                label="Proxy DLL"
                description="Automático usa a recomendação da Wiki ou detecta DX12/Vulkan"
                rgOptions={PROXY_CHOICES}
                selectedOption={opts.proxy ?? "auto"}
                onChange={(o) => setOpt({ proxy: o.data })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Streamline SDK (DLSSG via SL)"
                description="Baixa as DLLs NVIDIA Streamline para OptiScaler/streamline/ (ex.: Dawnwalker; desligue o VSync no jogo)"
                checked={Boolean(opts.streamline)}
                onChange={(v) => setOpt({ streamline: v })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="MFG Unlocker (plugin ASI)"
                description="Instala dashdogy/MFG-Unlocker em OptiScaler/plugins/MFGUnlocker.asi (XeMFG até 6X + aba MFG Unlocker no menu Insert)"
                checked={Boolean(opts.mfg_unlocker)}
                onChange={(v) => setOpt({ mfg_unlocker: v })}
              />
            </PanelSectionRow>
            {hasExtraTweaks(game.profile) && (
              <PanelSectionRow>
                <ToggleField
                  label="Ajustes do perfil (Wiki / Proton)"
                  description={summarizeProfileTweaks(game.profile)}
                  checked={opts.profile_tweaks !== false}
                  onChange={(v) => setOpt({ profile_tweaks: v })}
                />
              </PanelSectionRow>
            )}
            {game.re_engine && (
              <PanelSectionRow>
                <ToggleField
                  label="REFramework (nightly)"
                  description={
                    game.has_reframework && !game.installed
                      ? "Já existe um dinput8.dll no jogo; ele não será trocado"
                      : "Jogo RE Engine: sem ele o jogo pode fechar sozinho"
                  }
                  checked={opts.reframework}
                  onChange={(v) => setOpt({ reframework: v })}
                />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={!!busy || !state.ready || game.foreign} onClick={onInstall}>
                {busy ?? (game.installed ? "Aplicar / atualizar" : "Instalar")}
              </ButtonItem>
            </PanelSectionRow>
            {game.installed && (
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={!!busy} onClick={onUninstall}>
                  Remover deste jogo
                </ButtonItem>
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const listener = addEventListener<[version: string, games: string[]]>("gs_updated", (version, games) => {
    notify("GreenScaler", "ANR " + version + " aplicado em: " + games.join(", "));
  });

  return {
    name: "GreenScaler",
    titleView: <div className={staticClasses.Title}>GreenScaler</div>,
    content: <Content />,
    icon: <FaLeaf />,
    onDismount() {
      removeEventListener("gs_updated", listener);
    },
  };
});
