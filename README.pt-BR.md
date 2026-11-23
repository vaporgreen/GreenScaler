# GreenScaler (Plugin do Decky Loader)

[English](README.md) | **Português (Brasil)**

Plugin do **Decky Loader** para **SteamOS / Linux** (Steam Deck, Lenovo Legion Go, Bazzite, CachyOS) que instala e mantém atualizado o **OptiScaler nightly** + **ANR FG** (Apex Neural Rendering, geração de quadros) + **NVIDIA Streamline SDK** opcional + **MFG Unlocker (plugin ASI)** + **REFramework** em jogos Proton, com **FSR 4** ativado por padrão e perfis otimizados para portáteis.

> **O plugin não redistribui nada.** Ele baixa os lançamentos oficiais sob demanda e valida os checksums SHA-256 antes de alterar qualquer arquivo do jogo:
> - [optiscaler/OptiScaler-nightly](https://github.com/optiscaler/OptiScaler-nightly/releases) (`.7z`, conferido com o SHA-256 do release)
> - [apex-compute/ANR](https://github.com/apex-compute/ANR/releases) (`anr-fg-*-addon.zip`, conferido com `SHA256SUMS.txt`)
> - [NVIDIA-RTX/Streamline](https://github.com/NVIDIA-RTX/Streamline/releases) (port Linux do `Streamlined_fetcher_windows.bat` -> `OptiScaler/streamline/*.dll`)
> - [dashdogy/MFG-Unlocker](https://github.com/dashdogy/MFG-Unlocker/releases) (`MFGUnlocker.dll` conferido com `SHA256SUMS.txt` -> `OptiScaler/plugins/MFGUnlocker.asi` + `[Plugins] LoadAsiPlugins=true`)
> - [praydog/REFramework-nightly](https://github.com/praydog/REFramework-nightly/releases) (detectado automaticamente para jogos RE Engine da Capcom, tecla trocada para `Delete`)

## O que faz

- **Seleção automática do jogo:** escaneia todas as bibliotecas Steam, seleciona automaticamente o jogo aberto na biblioteca ou em execução, e detecta DLSS-G, anti-cheat e RE Engine.
- **Instalação limpa com backup e rollback:** instala o OptiScaler como `dxgi.dll` (ou `winmm.dll` em jogos puramente Vulkan), faz backup verificado por SHA-256 dos originais e restaura tudo ao remover.
- **Preserva suas opções de inicialização:** mescla `WINEDLLOVERRIDES`, `PROTON_ENABLE_NVAPI=1` e `PROTON_FSR4_UPGRADE=1` **sem apagar** o que você já usa (`--steamless`, `PROTON_VKD3D_LOWLATENCY=1`, etc.).
- **Padrões para portáteis:** em aparelhos com bateria (Steam Deck / Legion Go / ROG Ally), define automaticamente **FG 2X** e **FSR 4 INT8 Lite (`Fsr4Model=3`)**.
- **Streamline SDK (DLSSG via SL):** toggle com 1 clique para jogos que usam DLSS Frame Generation via Streamline (ex.: *The Blood of Dawnwalker*).
- **MFG Unlocker (`MFGUnlocker.asi`):** toggle com 1 clique para instalar o `dashdogy/MFG-Unlocker` em `OptiScaler/plugins/` (libera **XeMFG até 6X** em GPUs AMD/NVIDIA/Intel + aba **MFG Unlocker** no menu `Insert` do OptiScaler).
- **Atualização automática e retorno de versão:** busca versões novas no máximo a cada 6 horas, guarda o par anterior em cache e permite voltar com 1 clique.

## Instalação (Decky Loader)

1. Baixe **`GreenScaler.zip`** na [última release](https://github.com/vaporgreen/GreenScaler/releases/latest).
2. No Modo Jogo do SteamOS, vá em **Decky Loader → Configurações → Desenvolvedor → Instalar plugin a partir de um ZIP** e selecione `GreenScaler.zip`.
3. Abra o **GreenScaler** no menu `...` (QAM), clique em **Buscar atualização** uma vez, escolha o jogo e clique em **Instalar**.

## Requisitos

- **Proton-GE** ou **Proton-CachyOS** (ou Proton 10/11 com suporte a FSR 4) para `PROTON_FSR4_UPGRADE=1`.
- `7z` ou `bsdtar` no sistema (já vem no SteamOS).

## Compilar a partir do código-fonte

```sh
npm install
npm run build   # gera dist/index.js
```

Testar o backend Python sem o Decky:

```sh
PYTHONPATH=py_modules python3 -m greenscaler.core list
```

## Créditos

- **OptiScaler** — [optiscaler/OptiScaler](https://github.com/optiscaler/OptiScaler)
- **ANR FG** — [apex-compute/ANR](https://github.com/apex-compute/ANR)
- **MFG Unlocker** — [dashdogy/MFG-Unlocker](https://github.com/dashdogy/MFG-Unlocker)
- **Streamline SDK** — [NVIDIA-RTX/Streamline](https://github.com/NVIDIA-RTX/Streamline)
- **REFramework** — [praydog/REFramework](https://github.com/praydog/REFramework)
