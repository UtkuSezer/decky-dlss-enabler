<p align="center">
   <a href="https://ko-fi.com/B0B71HZTAX" target="_blank" rel="noopener noreferrer">
      <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support on Ko-fi"/>
   </a>
</p>

# Decky DLSS Enabler

Decky plugin for per-game DLSS Enabler management on Steam Deck.


For a selected Steam game, the plugin:

- locates the effective game executable directory
- copies the bundled DLSS Enabler proxy there as the selected DLL name
- backs up an existing stock DLL as `<name>.backup` when present
- writes a managed marker file for deterministic cleanup
- restores the original DLL on unpatch
- replaces Steam launch options while patched with:
  - `WINEDLLOVERRIDES=<method>=n,b`
  - `SteamDeck=0 %command%`
- restores the previous Steam launch options on unpatch
- optionally installs experimental FSR4 INT8 4.0.2b sidecar files (`amd_fidelityfx_dx12.dll`, `amd_fidelityfx_upscaler_dx12.dll`, and `OptiScaler.ini`)


bundled files:

- `bin/version.dll` (DLSS Enabler)
- version `4.4.0.2-dev`
- optional experimental FSR4 sidecar bundle:
  - `bin/amd_fidelityfx_dx12.dll`
  - `bin/amd_fidelityfx_upscaler_dx12.dll` (`4.0.2b`)

## Credits

Artur Graniszewski - [Dlss Enabler](https://github.com/artur-graniszewski/dlss-enabler-main)

OptiScaler team - [OptiScaler](https://github.com/optiscaler/OptiScaler)

Deck Wizard - Early testing, community support, [tutorial](https://github.com/artur-graniszewski/dlss-enabler-main) and showcase videos 
