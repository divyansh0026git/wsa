"""Exercise the serialized extension functions in a real Chromium document."""

import json
import subprocess
from pathlib import Path

import pytest

from wsa_agent.visual import _HeadlessCDPClient, find_chromium_binary


@pytest.fixture(scope="module")
def chromium():
    binary = find_chromium_binary()
    if not binary:
        pytest.skip("Chromium is required for the attachment submission check")
    with _HeadlessCDPClient(binary) as client:
        yield client


@pytest.mark.parametrize("provider", ["gemini", "claude", "chatgpt", "deepseek", "venice"])
def test_uploaded_file_and_prompt_post_exactly_once_after_composer_remount(chromium, provider):
    prefix = "_dom" if provider == "gemini" else "_" + provider
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output([
        "node", "-e",
        "const bg = require('./wsa-browser-bridge/background.js');"
        "console.log(JSON.stringify(process.argv.slice(1).map(name => bg[name].toString())))",
        "_installWsaDomUtilsFn", prefix + "InstallPageRuntimeFn", prefix + "StartTurnFn",
    ], cwd=root, text=True)
    utils, install, start = json.loads(source)
    editor = {
        "gemini": '<rich-textarea><div class="ql-editor" contenteditable="true" role="textbox"></div></rich-textarea>',
        "claude": '<div data-testid="chat-input" contenteditable="true" role="textbox"></div>',
        "chatgpt": '<div id="prompt-textarea" contenteditable="true" role="textbox"></div>',
        "deepseek": '<textarea id="chat-input"></textarea>',
        "venice": '<textarea aria-label="Chat message input"></textarea>',
    }[provider]
    button = {
        "gemini": '<gem-icon-button class="send-button"><button aria-label="Send message">Send</button></gem-icon-button>',
        "claude": '<button data-testid="send-button" aria-label="Send message">Send</button>',
        "chatgpt": '<button id="composer-submit-button" aria-label="Send prompt">Send</button>',
        "deepseek": '<div role="button" class="ds-button ds-button--primary ds-button--circle" aria-label="Send">Send</div>',
        "venice": '<button aria-label="Submit chat">Send</button>',
    }[provider]
    markup = '<form class="composer">' + editor + button + '</form>'
    if provider in ("claude", "deepseek"):
        markup = ('<main><input type="file" accept="audio/*"><input type="file" accept="image/png">'
                  '<div id="previews"></div><form><div class="input-inner">' + editor +
                  '</div>' + button + '</form></main>')
    chromium.evaluate(f"document.body.innerHTML = {json.dumps(markup)}")
    chromium.evaluate("""(() => {
      window.posts = [];
      window.pastes = 0;
      document.onpaste = event => {
        const file = event.clipboardData.files[0];
        if (!file) return;
        event.preventDefault();
        window.pastes++;
        const form = document.querySelector('form');
        const fresh = form.cloneNode(true);
        const chip = document.createElement('uploader-file-preview');
        chip.className = 'file-preview attachment-preview';
        chip.dataset.filename = chip.textContent = file.name;
        fresh.append(chip);
        form.replaceWith(fresh);
      };
      document.onsubmit = event => event.preventDefault();
      document.onclick = event => {
        if (!event.target.closest('button,[role=button]')) return;
        const box = document.querySelector('[contenteditable],textarea');
        const chip = document.querySelector('[data-filename]');
        window.posts.push([box.value ?? box.innerText, chip?.dataset.filename]);
        box.textContent = '';
        if ('value' in box) box.value = '';
        chip?.remove();
      };
    })()""")
    if provider in ("claude", "deepseek"):
        chromium.evaluate("""(() => {
          window.fileChanges = 0;
          document.onchange = event => {
            if (event.target.type !== 'file' || event.target.accept !== 'image/png') return;
            const file = event.target.files[0];
            window.fileChanges++;
            setTimeout(() => {
              const chip = document.createElement('span');
              chip.dataset.filename = file.name;
              if (document.querySelector('textarea')) chip.textContent = file.name;
              else chip.innerHTML = '<img src="/opaque-thumbnail">';
              document.querySelector('#previews').append(chip);
            }, 2600);
          };
        })()""")
    chromium.evaluate(f"({utils})()")
    chromium.evaluate(f"({install})()")
    result = chromium.evaluate(
        f"({start})('fixture', 'Inspect this image', "
        "[{data:'iVBORw0KGgo=', mime_type:'image/png', filename:'test.png'}])"
    )
    assert result["ok"], result
    if provider == "deepseek":
        # DeepSeek Web intentionally bypasses screenshot uploads in Phase 1 (text-only fallback)
        assert result["images_attached"] is False
        assert chromium.evaluate("[window.fileChanges, window.pastes]") == [0, 0]
        assert chromium.evaluate("window.posts") == [["Inspect this image", None]]
    else:
        assert result["images_attached"]
        if provider == "claude":
            assert chromium.evaluate("[window.fileChanges, window.pastes]") == [1, 0]
        else:
            assert chromium.evaluate("window.pastes") == 1
        assert chromium.evaluate("window.posts") == [["Inspect this image", "test.png"]]
