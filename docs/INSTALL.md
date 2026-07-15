# Installing Brevitas (`bvx`)

This guide explains **how Brevitas works**, how to **install it** on every
supported platform, and the commands you'll use to set it up, verify it, and
keep it running.

---

## How it works

Brevitas is middleware that sits between your AI coding assistants and the LLM
provider, trimming tokens on every request:

```
AI Tool → Brevitas Local Proxy → brevitas-systems (optimization) → LLM Provider → Response
        (127.0.0.1:8080)         (local socket)
```

There are three moving parts:

| Piece | What it is | Who manages it |
| --- | --- | --- |
| **`bvx`** | The installer/manager CLI (this repo, written in Go). Detects your AI tools, stores one API key, points each tool at the local proxy, and runs the background service. | You (`brew` / `install.ps1`) |
| **Proxy service** | A local HTTP proxy on `127.0.0.1:8080` that every configured tool routes through. Runs in the background (`bvx serve`). | `bvx` (installs + supervises it) |
| **`brevitas-systems`** | The Python package that holds the actual optimization logic. `bvx` talks to it over a local socket. **Not bundled** — `bvx` installs and pins it via `pip`. | `bvx install` / `bvx update` |

`bvx` never bundles the optimizer and never edits a tool config you haven't
approved. Every config change is backed up before it's rewritten.

---

## Requirements

- **macOS, Linux, or Windows** (x86-64 or ARM64).
- **Python 3.13+** — required by `brevitas-systems`. Homebrew installs it as a
  dependency automatically; on Windows install it yourself (e.g. from
  [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.13`).
- An account at [brevitassystems.com](https://brevitassystems.com) — you'll
  authorize it during setup and the device key is stored in your OS credential
  store (Keychain / Credential Manager / Secret Service).

You do **not** need a Go toolchain or a C compiler — every install path below
ships a prebuilt binary.

---

## Install

### macOS / Linux (Homebrew)

```sh
brew tap Brevitas-ai/brevitas
brew install bvx
```

Or as a single command:

```sh
brew install Brevitas-ai/brevitas/bvx
```

To build the latest `main` from source instead of a release binary:

```sh
brew install --HEAD Brevitas-ai/brevitas/bvx
```

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex
```

This downloads the prebuilt `bvx.exe` for your architecture, **verifies its
SHA-256** against the release `checksums.txt`, installs it to
`%LOCALAPPDATA%\Programs\bvx`, and adds that folder to your user `PATH`.

- Open a **new** terminal afterward so the updated `PATH` takes effect.
- To pin a specific version, set `$env:BVX_VERSION` before running:

  ```powershell
  $env:BVX_VERSION = "0.1.20"
  irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex
  ```

### Verify the binary is installed

```sh
bvx version
```

This only confirms the CLI is on your `PATH` — it does **not** configure
anything yet. That's the next step.

---

## First-time setup

Run the interactive installer once:

```sh
bvx install
```

This is the same as `bvx install ai`. Here's exactly what it does:

1. **Scans** your system for supported AI tools (Claude Code, Codex CLI,
   Continue, Aider, …).
2. **Opens the Brevitas dashboard** for one-click account authorization.
3. **Stores** the dedicated device key in your OS credential store.
4. **Rewrites** each supported tool's documented config to route through
   `http://127.0.0.1:8080` (backing up the original first).
5. **Installs and starts** the background services (proxy + `brevitas-systems`
   optimizer).
6. **Runs diagnostics** and prints a summary.

Example output:

```
Scanning system...

  ✓ Claude Code
  ✓ Codex CLI
  ✓ Continue
  ✓ Aider
  ⚠ Cursor (manual step required)
  ⚠ GitHub Copilot — Unsupported

Detected 4 configurable tool(s), 1 manual, 1 unsupported.

Opening https://brevitassystems.com/dashboard#bvx=...
Waiting for approval... approved

Installing...

  ✓ API key stored in macOS Keychain
  ✓ Claude Code configured
```

### Wiring up a codebase instead

To route every LLM call in a project through Brevitas (instead of configuring
interactive tools):

```sh
bvx install <repo>                 # scan + open the AI-call map
bvx install <repo> --apply         # write a .env.agentmap you can `source`
bvx install <repo> --apply --auto  # also rewrite hardcoded provider URLs
```

---

## Verify everything is working

```sh
bvx status     # proxy, service, and provider status
bvx doctor     # full diagnostics across the installation
```

If something looks off, re-apply config and restart the service:

```sh
bvx repair
```

---

## Managing the background service

```sh
bvx start      # start the proxy service
bvx stop       # stop it
bvx restart    # restart it
bvx logs       # print the proxy logs
bvx logs -f    # follow the logs live
```

---

## Updating

Upgrade the `bvx` CLI itself with your package manager:

```sh
# macOS / Linux
brew upgrade bvx

# Windows — just re-run the installer; it fetches the latest release
irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex
```

Upgrade the optimization engine (`brevitas-systems`):

```sh
bvx update
```

---

## Uninstalling

This restores every tool config from its backup and removes the background
service:

```sh
bvx uninstall
```

Then remove the CLI itself:

```sh
# macOS / Linux
brew uninstall bvx

# Windows
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Programs\bvx"
# and remove that folder from your user PATH (System Settings → Environment Variables)
```

---

## Command reference

| Command | Description |
| --- | --- |
| `bvx install` | Configure AI coding tools (`install ai`) or a codebase (`install <repo>`) |
| `bvx uninstall` | Restore all tool configs and remove the background service |
| `bvx status` | Show proxy, service, and provider status |
| `bvx stats` | Show cumulative token-savings metrics from the proxy |
| `bvx providers` | List supported providers and their detection/config state |
| `bvx doctor` | Run diagnostics across the whole installation |
| `bvx repair` | Re-apply configuration and restart the service |
| `bvx start` / `stop` / `restart` | Control the background proxy service |
| `bvx logs` | Print (or follow, with `-f`) the proxy logs |
| `bvx config` | Print or edit Brevitas configuration |
| `bvx login` / `logout` | Connect through the dashboard / remove the stored key |
| `bvx update` | Check for and upgrade the `brevitas-systems` package |
| `bvx version` | Print version information |

Run `bvx help` to see the full list at any time.

---

## Troubleshooting

- **`bvx: command not found` (Windows)** — open a new terminal; `PATH` updates
  only apply to shells started after install.
- **A tool still hits the provider directly** — run `bvx status` to confirm it
  was configured, then `bvx repair` to re-apply.
- **Optimizer won't start** — make sure Python 3.13+ is installed and on your
  `PATH`, then run `bvx update` followed by `bvx doctor`.
- **Anything else** — `bvx doctor` inspects the whole installation and points
  at the specific problem.

For how the proxy and optimizer communicate under the hood, see
[`PROTOCOL.md`](./PROTOCOL.md).
