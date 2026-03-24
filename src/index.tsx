import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useMemo, useState } from "react";
import { FaPlug } from "react-icons/fa";

type GameInfo = {
  appid: string;
  name: string;
  prefix_exists: boolean;
};

type GameListResponse = {
  status: "success" | "error";
  message?: string;
  games: GameInfo[];
};

type GameStatusResponse = {
  status: "success" | "error";
  message?: string;
  appid?: string;
  name?: string;
  prefix_exists?: boolean;
  patched?: boolean;
  method?: string | null;
  proxy_filename?: string | null;
  marker_name?: string;
  paths?: {
    compatdata: string;
    system32: string;
  };
};

type PatchResponse = {
  status: "success" | "error";
  message?: string;
  appid?: string;
  name?: string;
  method?: string;
  proxy_filename?: string;
  marker_name?: string;
  launch_options?: string;
  original_launch_options?: string;
  paths?: {
    compatdata: string;
    system32: string;
    proxy?: string;
    marker?: string;
  };
};

type UnpatchResponse = {
  status: "success" | "error";
  message?: string;
  launch_options?: string;
  paths?: {
    compatdata: string;
    system32: string;
  };
  notes?: string[];
};

const METHOD_OPTIONS = [
  { value: "version", label: "version.dll", hint: "Default for most games." },
  { value: "winmm", label: "winmm.dll", hint: "Good fallback when a game already uses version.dll." },
  { value: "d3d11", label: "d3d11.dll", hint: "Use for DirectX 11 games." },
  { value: "d3d12", label: "d3d12.dll", hint: "Use for DirectX 12 games." },
  { value: "dinput8", label: "dinput8.dll", hint: "Use for DirectInput hook paths." },
  { value: "dxgi", label: "dxgi.dll", hint: "Use for DXGI-based hook paths." },
  { value: "wininet", label: "wininet.dll", hint: "Use for games that respond to WinINet hooking." },
  { value: "winhttp", label: "winhttp.dll", hint: "Use for games that respond to WinHTTP hooking." },
  { value: "dbghelp", label: "dbghelp.dll", hint: "Use for Debug Help Library hook paths." },
] as const;

const listInstalledGames = callable<[], GameListResponse>("list_installed_games");
const getGameStatus = callable<[appid: string], GameStatusResponse>("get_game_status");
const patchGame = callable<
  [appid: string, method: string, currentLaunchOptions: string],
  PatchResponse
>("patch_game");
const unpatchGame = callable<[appid: string], UnpatchResponse>("unpatch_game");

const getMethodHint = (method: string) =>
  METHOD_OPTIONS.find((entry) => entry.value === method)?.hint ?? "";

const getAppLaunchOptions = (appid: number): Promise<string> =>
  new Promise((resolve, reject) => {
    let settled = false;
    let unregister = () => undefined;

    const timeout = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      unregister();
      reject(new Error("Timed out while loading the current launch options."));
    }, 5000);

    const registration = SteamClient.Apps.RegisterForAppDetails(appid, (details: { strLaunchOptions?: string }) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      unregister();
      resolve(details?.strLaunchOptions ?? "");
    });

    unregister = registration.unregister;
  });

const setAppLaunchOptions = (appid: number, launchOptions: string) => {
  SteamClient.Apps.SetAppLaunchOptions(appid, launchOptions);
};

let lastSelectedAppId = "";
let lastSelectedMethod = "dxgi";

function Content() {
  const [games, setGames] = useState<GameInfo[]>([]);
  const [gamesLoading, setGamesLoading] = useState(true);
  const [selectedAppId, setSelectedAppId] = useState<string>(() => lastSelectedAppId);
  const [selectedMethod, setSelectedMethod] = useState<string>(() => lastSelectedMethod);
  const [status, setStatus] = useState<GameStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [busyAction, setBusyAction] = useState<"patch" | "unpatch" | null>(null);
  const [resultMessage, setResultMessage] = useState<string>("");

  const loadGames = useCallback(async () => {
    setGamesLoading(true);
    setResultMessage("");
    try {
      const result = await listInstalledGames();
      if (result.status !== "success") {
        throw new Error(result.message || "Failed to load installed games.");
      }

      setGames(result.games);
      if (!result.games.length) {
        lastSelectedAppId = "";
        setSelectedAppId("");
        setStatus(null);
        return;
      }

      setSelectedAppId((current) => {
        const nextAppId = current && result.games.some((game) => game.appid === current)
          ? current
          : result.games[0].appid;
        lastSelectedAppId = nextAppId;
        return nextAppId;
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load installed games.";
      setResultMessage(`Error: ${message}`);
      toaster.toast({ title: "DLSS Enabler", body: message });
    } finally {
      setGamesLoading(false);
    }
  }, []);

  const loadStatus = useCallback(async (appid: string) => {
    if (!appid) {
      setStatus(null);
      return;
    }

    setStatusLoading(true);
    try {
      const result = await getGameStatus(appid);
      setStatus(result);
      if (result.status === "success" && result.method) {
        lastSelectedMethod = result.method;
        setSelectedMethod(result.method);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load game status.";
      setStatus({ status: "error", message });
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadGames();
  }, [loadGames]);

  useEffect(() => {
    if (!selectedAppId) {
      setStatus(null);
      return;
    }
    void loadStatus(selectedAppId);
  }, [selectedAppId, loadStatus]);

  const selectedGame = useMemo(
    () => games.find((game) => game.appid === selectedAppId) ?? null,
    [games, selectedAppId],
  );

  const selectedMethodLabel = useMemo(
    () => METHOD_OPTIONS.find((entry) => entry.value === selectedMethod)?.label ?? `${selectedMethod}.dll`,
    [selectedMethod],
  );

  const canPatch = Boolean(selectedGame && status?.status === "success" && status.prefix_exists && !busyAction);
  const canUnpatch = Boolean(selectedGame && status?.status === "success" && status.marker_name && !busyAction);

  const patchButtonLabel = useMemo(() => {
    if (busyAction === "patch") return "Patching...";
    if (!selectedGame) return "Patch selected game";
    if (!status?.prefix_exists) return "Patch target not found";
    if (status?.method && status.method !== selectedMethod) return `Switch to ${selectedMethodLabel}`;
    if (status?.marker_name) return `Reinstall ${selectedMethodLabel}`;
    return `Patch with ${selectedMethodLabel}`;
  }, [busyAction, selectedGame, selectedMethodLabel, selectedMethod, status]);

  const handlePatch = useCallback(async () => {
    if (!selectedGame || !selectedAppId) return;

    setBusyAction("patch");
    setResultMessage("");
    try {
      const currentLaunchOptions = await getAppLaunchOptions(Number(selectedAppId));
      const result = await patchGame(selectedAppId, selectedMethod, currentLaunchOptions);
      if (result.status !== "success") {
        throw new Error(result.message || "Patch failed.");
      }

      setAppLaunchOptions(Number(selectedAppId), result.launch_options || "");
      setResultMessage(result.message || `Patched ${selectedGame.name} using ${selectedMethodLabel}.`);
      toaster.toast({
        title: "DLSS Enabler",
        body: result.message || `Patched ${selectedGame.name} using ${selectedMethodLabel}.`,
      });
      await loadStatus(selectedAppId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Patch failed.";
      setResultMessage(`Error: ${message}`);
      toaster.toast({ title: "DLSS Enabler", body: message });
    } finally {
      setBusyAction(null);
    }
  }, [loadStatus, selectedAppId, selectedGame, selectedMethod, selectedMethodLabel]);

  const handleUnpatch = useCallback(async () => {
    if (!selectedGame || !selectedAppId) return;

    setBusyAction("unpatch");
    setResultMessage("");
    try {
      const result = await unpatchGame(selectedAppId);
      if (result.status !== "success") {
        throw new Error(result.message || "Unpatch failed.");
      }

      setAppLaunchOptions(Number(selectedAppId), result.launch_options || "");
      setResultMessage(result.message || `Unpatched ${selectedGame.name}.`);
      toaster.toast({
        title: "DLSS Enabler",
        body: result.message || `Unpatched ${selectedGame.name}.`,
      });
      await loadStatus(selectedAppId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unpatch failed.";
      setResultMessage(`Error: ${message}`);
      toaster.toast({ title: "DLSS Enabler", body: message });
    } finally {
      setBusyAction(null);
    }
  }, [loadStatus, selectedAppId, selectedGame]);

  const statusMessage = useMemo(() => {
    if (!selectedGame) return "Choose a game to manage its patch state.";
    if (statusLoading) return "Loading patch status...";
    if (!status) return "No status loaded yet.";
    if (status.status === "error") return `Error: ${status.message || "Failed to load status."}`;
    return status.message || "Ready.";
  }, [selectedGame, status, statusLoading]);

  return (
    <PanelSection>
      <PanelSectionRow>
        <DropdownItem
          label="Target game"
          menuLabel="Installed Steam games"
          strDefaultLabel={gamesLoading ? "Loading installed games..." : "Choose a game"}
          disabled={gamesLoading || games.length === 0}
          selectedOption={selectedAppId}
          rgOptions={games.map((game) => ({
            data: game.appid,
            label: game.prefix_exists ? game.name : `${game.name} (target not found)`,
          }))}
          onChange={(option) => {
            const nextAppId = String(option.data);
            lastSelectedAppId = nextAppId;
            setSelectedAppId(nextAppId);
            setResultMessage("");
          }}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Game">{selectedGame?.name ?? "—"}</Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="App ID">{selectedGame?.appid ?? "—"}</Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Target ready">
          {selectedGame && status?.status === "success" ? (status.prefix_exists ? "Yes" : "No") : "—"}
        </Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Patched">
          {selectedGame && status?.status === "success" ? (status.patched ? "Yes" : "No") : "—"}
        </Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Current DLL name">
          {selectedGame && status?.status === "success" && status.method
            ? (status.proxy_filename || `${status.method}.dll`)
            : "—"}
        </Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Status">{statusMessage}</Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <DropdownItem
          label="Injection method"
          description={getMethodHint(selectedMethod)}
          menuLabel="Injection method"
          strDefaultLabel="Choose DLL name"
          selectedOption={selectedMethod}
          rgOptions={METHOD_OPTIONS.map((entry) => ({ data: entry.value, label: entry.label }))}
          onChange={(option) => {
            const nextMethod = String(option.data);
            lastSelectedMethod = nextMethod;
            setSelectedMethod(nextMethod);
          }}
          disabled={!selectedGame || busyAction !== null}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <Field label="Selected DLL name" description="The bundled DLSS Enabler proxy will be copied into the chosen game executable directory using this filename.">
          {selectedMethodLabel}
        </Field>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handlePatch} disabled={!canPatch}>
          {patchButtonLabel}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleUnpatch} disabled={!canUnpatch}>
          {busyAction === "unpatch" ? "Unpatching..." : "Unpatch selected game"}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => selectedAppId && void loadStatus(selectedAppId)} disabled={!selectedAppId || busyAction !== null || statusLoading}>
          {statusLoading ? "Refreshing..." : "Refresh selected game status"}
        </ButtonItem>
      </PanelSectionRow>

      {resultMessage ? (
        <PanelSectionRow>
          <Field label="Last action">
            <div>
              {resultMessage.split("\n").map((line, index) => (
                <div key={`${line}-${index}`}>{line || "\u00A0"}</div>
              ))}
            </div>
          </Field>
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}

export default definePlugin(() => {
  console.log("DLSS Enabler frontend loaded");

  return {
    name: "DLSS Enabler",
    titleView: <div className={staticClasses.Title}>DLSS Enabler</div>,
    content: <Content />,
    alwaysRender: true,
    icon: <FaPlug />,
    onDismount() {
      console.log("DLSS Enabler frontend unloaded");
    },
  };
});
