# WSA — Web Server Agent

<p align="left">
  <a href="https://github.com/divyansh0026git/wsa/actions/workflows/wsa-smoke.yml"><img src="https://github.com/divyansh0026git/wsa/actions/workflows/wsa-smoke.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-lightgrey.svg" alt="Platform: Windows | macOS">
  <img src="https://img.shields.io/badge/Browsers-Chrome%20%7C%20Brave-orange.svg" alt="Browsers: Chrome | Brave">
</p>

A local terminal coding assistant that connects your command line to supported AI websites in Chrome or Brave through a lightweight browser extension. WSA inspects workspaces, edits files with undo checkpoints, runs commands and tests with your approval, and coordinates multi-step coding work across open browser tabs.

## Highlights

- **No API Keys Required**: Works directly through your existing signed-in web sessions in Chrome or Brave.
- **5 Supported Web Bridges**: Supports Claude, ChatGPT, Gemini, DeepSeek, and Venice.
- **Background Tab Relay**: Streams responses without stealing browser focus or losing active turns to tab discarding.
- **Multiagent Auto-Failover**: Preserves session context and rolls over to your next queued provider tab when an account hits its usage limit.
- **Supervised Approval Gate**: Starts in `smart` mode—reads and ordinary workspace edits proceed automatically while shell commands, Python execution, deletions, and network/browser actions require per-action approval.
- **Adaptive 4-Role Orchestration**: Built-in Explorer → Architect → Coder → Verifier workflow with dependency-ordered plans (`/plan`) and independent verification (`/verify`).

## Supported Web Bridges & Models

| Provider | Web Endpoint | Models & Thinking Modes |
| :--- | :--- | :--- |
| **Claude** | `claude.ai` | Claude Sonnet 5.5 Extra, Claude Opus 5.5, Claude Fable 5.2, Claude Mythos 5.2 |
| **ChatGPT** | `chatgpt.com` | GPT-6 Astra, GPT-6.1 Astra, GPT-6.1 Sol / Terra, o3, o3-pro |
| **Gemini** | `gemini.google.com` | Gemini 4 Carbon, Gemini 4 Argon, Gemini 4 Hydrogen (1M Context, xhigh thinking) |
| **DeepSeek** | `chat.deepseek.com` | DeepSeek-V4-Pro, DeepSeek-V4.1-Flash, DeepSeek-R1 (DeepThink) |
| **Venice** | `venice.ai` | DeepSeek V4.1 Flash Uncensored (524K context) |

*Select your exact model, web search toggle, and thinking mode directly inside the website's tab.*

## Requirements

- **OS & Runtime**: Windows 10/11 or macOS, Python 3.10+, and Git.
- **Browser**: Chrome or Brave with a signed-in account for each website you want to use.
- **Optional**: Node.js (only required to run browser extension tests).

Keep the cloned folder in a stable location because both the installed `wsa` command and unpacked browser extension reference it.

## Installation

### Windows

Install Python from [python.org](https://www.python.org/downloads/) and Git, then run in PowerShell:

```powershell
git clone https://github.com/divyansh0026git/wsa.git wsa
cd wsa
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer creates `.venv` and installs WSA without requiring administrator access or editing your PATH. Add the printed `.venv\Scripts` folder to your **user PATH** in Windows Environment Variables (or invoke the full path directly), open a new terminal, and verify with `wsa --version`.

### macOS

Install Python 3.10+ and Git, then run in Terminal:

```sh
git clone https://github.com/divyansh0026git/wsa.git ~/wsa
cd ~/wsa
sh scripts/install.sh
```

Add the printed `.venv/bin` path to `~/.zshrc` (or your bash startup file), open a new terminal, and verify with `wsa --version`:

```sh
export PATH="$HOME/wsa/.venv/bin:$PATH"
```

Set `PYTHON=/path/to/python3` before running `scripts/install.sh` if you need a specific Python executable.

## Browser Setup

1. Run `wsa` in a terminal (the local server starts automatically on `127.0.0.1:8081`).
2. Open `chrome://extensions` or `brave://extensions`, enable **Developer mode**, click **Load unpacked**, and select the `wsa-browser-bridge` folder inside your clone.
3. Open and sign in to any supported AI website tab, select your model/thinking mode there, and keep the tab open.
4. Click the **WSA** extension icon and press **Sync**. See [wsa-browser-bridge/SETUP.md](wsa-browser-bridge/SETUP.md) for detailed relay notes.
5. In any project directory, run `wsa` (or `wsa --model claude`). Use `/models` to switch bridges and `/account` to inspect connected browsers.

> The extension requests broad website, tab, and cookie access to bridge tabs and run browser tools. Use it only in a browser profile you trust, never publish local state or session logs, and keep the server bound to localhost. See [OPEN-SOURCE.md](OPEN-SOURCE.md).

## CLI Options

```sh
wsa [options] [query ...]
```

| Option | Description |
| :--- | :--- |
| `-p`, `--project PATH` | Target project directory (default: current working directory) |
| `-m`, `--model NAME` | Web bridge: `claude`, `chatgpt`, `gemini`, `deepseek`, or `venice` (default: `gemini`) |
| `-a`, `--account NAME` | Bind a specific connected browser relay (e.g., `chrome`, `brave`) |
| `-c`, `--continue` | Resume the last saved workspace session and browser chat thread |
| `--permissions MODE` | Approval policy: `smart` (default), `strict`, or `auto` |
| `--full-permissions` | Alias for `--permissions auto` (bypasses approval prompts for the current run only) |
| `--api-base URL` | Local server API base URL (default: `http://127.0.0.1:8081/v1`) |
| `--api-key KEY` | Local server API key (default: `sk-wsa`) |
| `--no-server` | Do not auto-start the local backend server |
| `--models` | List available web bridges and exit |
| `-v`, `--version` | Show the installed WSA version and exit |

## Interactive Commands & Shortcuts

- **Session**: `/new [keep]`, `/clear`, `/compact`, `/save`, `/load`, `/status`, `/tokens`, `/theme [name]`, `/help`, `/exit`
- **Web Bridges & Browser**: `/model [name]`, `/models`, `/singleagent [model]`, `/multiagent`, `/account [name]`
- **Workspace**: `/init`, `/project [dir]`, `/read <path>`, `/run <cmd>`, `/search <pattern>`, `/diff`, `/undo`
- **Orchestration & Controls**: `/team [auto|on|off|single-chat|role-chats|dual|single]`, `/plan [task]`, `/verify [scope]`, `/perm [smart|strict|auto]`, `/tools`, `/rules`
- **Shortcuts**: `Shift+Tab` cycles permission mode, `?` shows keyboard shortcuts, `@path` attaches workspace files, and `Ctrl+C` interrupts the active turn cleanly.

## Built-in task skills and orchestrator output

WSA selects local skill excerpts from the original request. UI, interface, dashboard,
website, and frontend work uses Anthropic's
[frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)
together with Damir Sokolovsky's [unslop](https://github.com/heydamir/unslop) by default.
Feature implementation uses [test-driven-development](https://github.com/obra/superpowers/tree/main/skills/test-driven-development).
Crashes, failing tests, and repair passes use [systematic-debugging](https://github.com/obra/superpowers/tree/main/skills/systematic-debugging).
Relevant skills can run together. WSA prints the selection once and passes it to each scoped
role. Naming a skill explicitly also selects it. Free mode omits the skills.

The upstream Markdown and license files ship under `wsa_agent/skills/`. Anthropic's
skill uses Apache-2.0, `unslop` uses CC-BY-4.0 (credit: Damir Sokolovsky / `@heydamir`),
and the two Superpowers skills use MIT. Updates are distributed with WSA. Selecting a skill
works offline and adds no model round-trip.

Orchestrator mode streams the web provider's available response text, keeps stage output
in terminal history, and shows tool calls, returned line ranges, and short file previews.
A coder whose browser response times out before its first tool call can retry once.
If it still fails, the report includes the worker error and the tasks waiting on it.

## Permissions & Safety

WSA starts in **smart** mode. File reads and ordinary workspace edits proceed automatically, while shell commands, Python/test execution, deletions, rollbacks, protected config edits, custom tools, and browser/network actions prompt for one-time approval showing the exact action and arguments. Subagents inherit the same approval policy; denying an action stops dependent steps, and noninteractive runs fail closed when approval is required.

- **Strict** (`--permissions strict` or `/perm strict`): Also requires approval for ordinary file edits.
- **Smart** (`--permissions smart` or `/perm smart`): Default supervised coding mode.
- **Auto / Bypass** (`--full-permissions`, `--permissions auto`, or `/perm auto`): Bypasses approval prompts for the current run only (never auto-restored when resuming saved sessions). Uses your OS account's normal privileges while keeping workspace/worker boundaries and process guards active. Use only for trusted tasks in an isolated environment.

These controls are **not an OS sandbox**: approved commands run with your account's filesystem and network access. Worker file ownership is enforced for structured edits, while shell commands cannot be confined by those path checks; workers and overlapping turns in one process therefore run sequentially (use separate workspaces for concurrent CLI processes). The verifier reports success only with a valid final JSON verdict backed by locally executed verification evidence.

## Testing & Verification

Run the Python test suite:

```sh
pytest -v
```

Syntax-check extension scripts (requires Node.js):

```sh
node --check wsa-browser-bridge/background.js
node --check wsa-browser-bridge/content_script_bridge.js
node --check wsa-browser-bridge/content_script_main.js
node --check wsa-browser-bridge/popup.js
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

WSA builds on [gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) and the work of its contributors. See the MIT license.

<!-- Discovery Index: sonnet 5.5, sonnet 5.5 extra, claude sonnet 5.5, claude sonnet 5, sonnet 5, opus 5.5, claude opus 5.5, fable 5.2, fable 5.1, claude fable 5.2, mythos, mythos 5.2, mythos 5.1, claude mythos 5.2, gpt 6, gpt 6.1, gpt-6, gpt-6.1, gpt 6 astra, gpt 6.1 astra, gpt-6 astra, gpt-6.1 astra, gpt-6 sol, gpt-6.1 sol, gpt-6.1 terra, gpt-6 luna, deepseek v4, deepseek v4.1, deepseek v4.1 pro, deepseek-v4-pro, deepseek-v4.1-flash, gemini 4, gemini 4 carbon, gemini 4 argon, gemini 4 hydrogen, carbon 4, argon 4, hydrogen 4, gemini-4-carbon, gemini-4-argon, gemini-4-hydrogen, gemini 3.1 deep think, gemini 3.8 flash, o3, o3-pro, o3-mini, o1 pro, deepseek r1, deepseek v3, venice uncensored, coding agent, cli web agent, autonomous browser bridge -->
