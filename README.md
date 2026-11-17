# WSA — Web Server Agent

A local coding assistant that connects your terminal to supported AI websites in Chrome or Brave through a browser extension. WSA can inspect a project, propose and apply changes, run commands with your approval, and coordinate multi-step work.

**Supported web agents:** ChatGPT, Claude, Gemini, DeepSeek and Venice. Availability and behavior depend on the website, your account and the browser. Select the website's model and thinking mode in its tab.

## Requirements

- Windows 10/11 or macOS, Python 3.10 or newer, and Git.
- Chrome or Brave and a signed-in account for each website you want to use.
- Internet access to install the Python packages and use the websites. Node.js is optional and is only needed for extension tests.

Keep the cloned folder in a stable location: the installed command and browser extension reference it. This project includes an MIT license; see [LICENSE](LICENSE).

## Install on Windows

Install Python from [python.org](https://www.python.org/downloads/) and Git, then open PowerShell:

```powershell
git clone https://github.com/divyansh0026git/wsa.git wsa
cd wsa
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer creates `.venv` and installs WSA and its Python dependencies. Add the displayed `.venv\Scripts` folder to your **user PATH** in Windows Environment Variables, then open a new terminal. Run `wsa --version` to check it. The installer does not change your PATH or require administrator access. You can instead invoke the displayed full path directly.

## Install on macOS

Install Python 3.10 or newer and Git, then in Terminal:

```sh
git clone https://github.com/divyansh0026git/wsa.git ~/wsa
cd ~/wsa
sh scripts/install.sh
```

The installer creates `.venv` and installs WSA and its Python dependencies. For the standard macOS zsh shell, add the following line to `~/.zshrc` and open a new terminal:

```sh
export PATH="$HOME/wsa/.venv/bin:$PATH"
```

If you cloned elsewhere, use the `.venv/bin` path printed by the installer instead. Run `wsa --version` to check it. For bash, add the line to your shell's startup file instead. Set `PYTHON` before running the installer to select a particular Python executable if necessary.

## Connect the browser

1. Start `wsa` in a terminal. It normally starts its local server automatically.
2. Open `chrome://extensions` or `brave://extensions`, enable Developer mode, and choose **Load unpacked**. Select the `wsa-browser-bridge` folder in your clone.
3. Sign in to the supported website you want to use, keep its tab open, and select the desired model there.
4. Open the WSA extension popup and press **Sync**. See [browser setup](wsa-browser-bridge/SETUP.md) for details.
5. In any project directory, run `wsa` or `wsa --model chatgpt`. Use `/models` to choose a bridge and `/account` to inspect connected browsers. `wsa --project PATH` selects a different workspace.

The extension requests broad website, tab and cookie access to provide its bridge and browser tools. Use it only in a browser profile you trust. WSA stores local runtime state and may contain account information; never publish your configuration, cookies, session data or logs. The server defaults to localhost; do not expose it to a network without securing it. See [open-source release checklist](OPEN-SOURCE.md).

## Testing & Verification

### Permissions and unattended runs

WSA starts in **smart** mode. Reads and ordinary workspace file edits proceed automatically.
Running shell commands, Python, tests, background services or custom tools requires approval,
as do deletion, rollback, protected configuration edits, and browser/network actions. The
prompt shows the complete action and approves it **once**. Subagents use the same approval
controls. A denial stops the turn and blocks dependent actions; a noninteractive run cannot
grant approval and exits unsuccessfully when approval is needed.

- `wsa --permissions strict` or `/perm strict`: also approve ordinary file edits.
- `wsa --permissions smart` or `/perm smart`: normal supervised coding.
- `wsa --full-permissions` (alias: `wsa --permissions auto`) or `/perm auto`: explicitly bypass approvals for the current run. The TUI footer shows the active permission mode alongside full model and account details. This uses your operating system account’s existing access, does not elevate OS privileges, and preserves workspace/worker boundaries and process self-preservation guards. Saved sessions do not silently re-enable bypass.
  Use this only for trusted tasks in an appropriately isolated environment. Saved sessions
  never restore bypass automatically.

These controls are **not an operating-system sandbox**. Approved code runs with your account's
filesystem and network access. Worker ownership is enforced for structured file edits;
shell commands cannot be confined by those path checks. Workers and overlapping turns in
one Python process therefore run sequentially, including `/team dual`. Separate CLI processes
should use separate workspaces if running concurrently.

A verifier can report success only with a valid final JSON verdict and local verification
evidence. Failed or missing verification and denied approvals produce unsuccessful results.

Run the test suite:

```bash
pytest -v
```

Syntax-check extension scripts (requires Node.js):

```bash
node --check wsa-browser-bridge/background.js
node --check wsa-browser-bridge/content_script_bridge.js
node --check wsa-browser-bridge/content_script_main.js
node --check wsa-browser-bridge/popup.js
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

WSA builds on [gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) and the work of its contributors. See the MIT license.
