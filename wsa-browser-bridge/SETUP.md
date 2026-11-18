# WSA — Web Browser Bridge Setup

> Engineered by Divyansh Saraf

MV3 browser bridge connecting your open **Brave** or **Chrome** tabs (ChatGPT, Claude, Gemini, DeepSeek, and Venice) with **WSA** (`wsa`) and the local server on `http://127.0.0.1:8081`.

## Install

1. Open `brave://extensions` or `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** and select `wsa-browser-bridge`
4. Open `https://chatgpt.com`, `https://claude.ai`, `https://gemini.google.com/app`, `https://chat.deepseek.com`, or `https://venice.ai`, sign in, and pick your exact model & thinking mode directly inside the website UI
5. Open the **WSA** extension popup and click **Sync**

The popup auto-detects and displays:
- **Browser**: Brave or Chrome
- **Website**: `chatgpt.com`, `claude.ai`, `gemini.google.com`, `chat.deepseek.com`, or `venice.ai`
- **Account**: Signed-in email / account
- **Model**: Active web model selected in that tab

## Use in WSA CLI

1. Start `wsa` in your workspace (or run `wsa.bat` / `wsa.ps1`).
2. Switch bridges with `/model chatgpt`, `/model claude`, `/model gemini`, `/model deepseek`, or `/model venice`.
3. Inspect or switch browsers with `/account`, `/account chrome`, or `/account brave`.

## Updating to 2.0.2 and checking background tabs

1. Let any current request finish. Restart the WSA server to load the relay changes.
2. Reload **WSA - Web Browser Bridge** on the browser's extensions page; verify version **2.0.2**.
3. Refresh the AI tabs once to load the updated content script, then click **Sync**.
4. Start a WSA request and immediately switch to an unrelated tab. Stay there through completion; WSA should stream and finish without activating the AI tab.
5. Repeat for each provider you use, including a request lasting over two minutes. Check that the terminal receives the final answer and accepts the next request.

The shared worker polls the saved tab ID and uses browser alarms to recover without page keepalive messages. It prevents automatic tab discarding and preserves pending results until the server acknowledges them. An explicit inspection/screenshot command may still activate its target tab; ordinary AI relay work does not.

Keep the browser and provider tabs open. Closing a tab, signing out, browser/OS suspension, or provider errors can still interrupt work. Worker recovery during uncertain prompt submission polls the existing turn instead of submitting a duplicate prompt; a restart before submission is reported as an explicit retryable failure.

Automated lifecycle checks: `python -m pytest tests/test_background_tab_relay.py -q` (requires Node.js). These simulate browser APIs; the tab-switch procedure above is the live acceptance check.

Version 2.0.2 sends busy heartbeats without scanning AI page DOMs, including when an outstanding page poll is blocked. If script injection returns no submission acknowledgement, it polls the saved request instead of declaring submission failed or sending the prompt again. Explicit page errors still surface as errors; a page turn that never appears still times out.
