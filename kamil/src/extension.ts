import * as vscode from 'vscode';

const KAMIL_URL = 'http://127.0.0.1:3000';

export function activate(context: vscode.ExtensionContext) {
    const provider = new KamilChatProvider(context.extensionUri);

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('kamil.chatView', provider)
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('kamil.openChat', () => {
            vscode.commands.executeCommand('workbench.view.extension.kamil-sidebar');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('kamil.askAboutSelection', () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) return;

            const selectedText = editor.document.getText(editor.selection);
            const filePath = editor.document.fileName;
            const language = editor.document.languageId;

            if (!selectedText) {
                vscode.window.showInformationMessage('Select some code first');
                return;
            }

            const message = `File: ${filePath}\nLanguage: ${language}\n\nSelected code:\n\`\`\`${language}\n${selectedText}\n\`\`\`\n\nQuestion: `;
            provider.sendMessage(message);
            vscode.commands.executeCommand('workbench.view.extension.kamil-sidebar');
        })
    );
}

class KamilChatProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;

    constructor(private readonly _extensionUri: vscode.Uri) { }

    public sendMessage(text: string) {
        if (this._view) {
            this._view.webview.postMessage({ type: 'prefill', text });
        }
    }

    private _getEditorContext() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) return { file: null, language: null, selection: null, project: null };

        const filePath = editor.document.fileName;
        const reposPath = 'C:\\Users\\adnan\\Documents\\repos\\';
        let project: string | null = null;

        if (filePath.toLowerCase().startsWith(reposPath.toLowerCase())) {
            const relative = filePath.substring(reposPath.length);
            project = relative.split('\\')[0];
        }

        return {
            file: filePath,
            language: editor.document.languageId,
            selection: editor.document.getText(editor.selection) || null,
            lineCount: editor.document.lineCount,
            project
        };
    }

    private _buildMessage(text: string): string {
        const editor = vscode.window.activeTextEditor;
        if (!editor) return text;

        const filePath = editor.document.fileName;
        const language = editor.document.languageId;
        const selection = editor.document.getText(editor.selection);
        const reposPath = 'C:\\Users\\adnan\\Documents\\repos\\';
        let project: string | null = null;

        if (filePath.toLowerCase().startsWith(reposPath.toLowerCase())) {
            const relative = filePath.substring(reposPath.length);
            project = relative.split('\\')[0];
        }

        if (selection && selection.trim().length > 0) {
            return `Current file: ${filePath} (${language})\nProject: ${project || 'unknown'}\nSelected code:\n\`\`\`${language}\n${selection}\n\`\`\`\n\n${text}`;
        } else {
            return `Current file: ${filePath} (${language})\nProject: ${project || 'unknown'}\n\n${text}`;
        }
    }

    public resolveWebviewView(webviewView: vscode.WebviewView) {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: []
        };

        webviewView.webview.html = this._getHtml();

        let lastProject: string | null = null;  // ADDED

        webviewView.webview.onDidReceiveMessage(async (data) => {
            if (data.type === 'getContext') {
                const ctx = this._getEditorContext();
                webviewView.webview.postMessage({ type: 'context', ...ctx });

                // UPDATED LOGIC
                if (ctx.project && ctx.project !== lastProject) {
                    lastProject = ctx.project;
                    try {
                        await fetch(`${KAMIL_URL}/select_project`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ project_name: ctx.project })
                        });
                    } catch { }
                }

            } else if (data.type === 'chat') {
                const fullMessage = this._buildMessage(data.text);

                if (data.mode === 'council') {
                    try {
                        const res = await fetch(`${KAMIL_URL}/council`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ content: fullMessage })
                        });
                        const result = await res.json();
                        webviewView.webview.postMessage({ type: 'response', data: result, mode: 'council' });
                    } catch (e: any) {
                        webviewView.webview.postMessage({ type: 'error', message: e.message });
                    }
                } else {
                    try {
                        const res = await fetch(`${KAMIL_URL}/chat_agentic`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ content: fullMessage })
                        });

                        webviewView.webview.postMessage({ type: 'stream_start' });

                        const reader = (res.body as any).getReader();
                        const decoder = new TextDecoder();
                        let buffer = '';

                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) break;
                            buffer += decoder.decode(value, { stream: true });
                            const lines = buffer.split('\n');
                            buffer = lines.pop() || '';
                            for (const line of lines) {
                                if (!line.startsWith('data: ')) continue;
                                const chunk = line.slice(6);
                                if (chunk === '[DONE]') {
                                    webviewView.webview.postMessage({ type: 'stream_end' });
                                    return;
                                }
                                try {
                                    const parsed = JSON.parse(chunk);
                                    if (parsed.chunk) {
                                        webviewView.webview.postMessage({ type: 'stream_chunk', chunk: parsed.chunk });
                                    }
                                    if (parsed.pending_confirmation) {
                                        webviewView.webview.postMessage({ type: 'pending_confirmation', actions: parsed.pending_confirmation });
                                    }
                                    if (parsed.error) {
                                        webviewView.webview.postMessage({ type: 'error', message: parsed.error });
                                    }
                                } catch { }
                            }
                        }
                        webviewView.webview.postMessage({ type: 'stream_end' });
                    } catch (e: any) {
                        console.error('Kamil fetch error:', e);
                        webviewView.webview.postMessage({ type: 'error', message: e.message });
                    }
                }
            }
        });
    }

    private _getHtml(): string {
        return `<!DOCTYPE html>

<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';">
<title>Kamil</title>
<style>
  :root {
    --bg: #080808;
    --surface: #0f0f0f;
    --border: #1c1c1c;
    --text: #d4d4d4;
    --text-muted: #525252;
    --text-dim: #2a2a2a;
    --accent: #3b82f6;
    --council-accent: #7c3aed;
    --code-bg: #0a0a0a;
    --input-bg: #0f0f0f;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--vscode-font-family, 'Segoe UI', sans-serif);
    font-size: 12px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  #ctx-bar {
    padding: 5px 10px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 10px;
    color: var(--text-muted);
    font-family: 'Cascadia Code', 'Consolas', monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    letter-spacing: 0.05em;
    flex-shrink: 0;
  }

  #ctx-bar.active { color: var(--accent); }

  #messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  #messages::-webkit-scrollbar { width: 2px; }
  #messages::-webkit-scrollbar-thumb { background: var(--border); }

  .msg-row {
    padding: 0 10px;
    flex-shrink: 0;
  }

  .msg-row.user { display: flex; justify-content: flex-end; }
  .msg-row.system-row { display: flex; justify-content: center; }

  .bubble {
    max-width: 90%;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .bubble.user {
    color: var(--text-muted);
    font-size: 11px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px 6px 1px 6px;
    padding: 6px 10px;
  }

  .bubble.assistant {
    color: var(--text);
    font-size: 12px;
    border-left: 2px solid var(--accent);
    padding: 8px 10px;
    width: 100%;
  }

  .bubble.assistant.council { border-left-color: var(--council-accent); }

  .role-tag {
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 8px;
    letter-spacing: 0.15em;
    margin-bottom: 4px;
    display: block;
    color: var(--accent);
  }

  .role-tag.council { color: var(--council-accent); }

  .system-note {
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 9px;
    color: var(--text-dim);
    letter-spacing: 0.1em;
    padding: 4px 0;
  }

  /* Council block */
  .council-block {
    margin: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    flex-shrink: 0;
  }

  .c-label {
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 8px;
    letter-spacing: 0.12em;
    padding: 5px 10px;
    border-bottom: 1px solid var(--border);
  }

  .c-label.analyst { color: #22c55e; background: #080d08; }
  .c-label.advocate { color: #f97316; background: #0d0800; }
  .c-label.synth { color: var(--council-accent); background: #0a080f; }

  .c-body {
    padding: 8px 10px;
    font-size: 11px;
    line-height: 1.6;
    max-height: 130px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .c-body.analyst { color: #a3d9a5; background: #060b06; }
  .c-body.advocate { color: #d4a0a0; background: #0a0500; }
  .c-body.synth { color: var(--text); background: #08060d; }

  .c-body::-webkit-scrollbar { width: 2px; }
  .c-body::-webkit-scrollbar-thumb { background: var(--border); }

  .cursor {
    display: inline-block;
    width: 1.5px;
    height: 12px;
    background: var(--accent);
    margin-left: 1px;
    vertical-align: middle;
    animation: blink 1s infinite;
  }

  @keyframes blink { 0%,49%{opacity:1} 50%,100%{opacity:0} }

  /* Input */
  #input-area {
    border-top: 1px solid var(--border);
    padding: 8px;
    flex-shrink: 0;
    background: var(--bg);
  }

  #input {
    width: 100%;
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text);
    padding: 7px 32px 7px 10px;
    font-size: 11px;
    font-family: inherit;
    resize: none;
    min-height: 34px;
    max-height: 120px;
    outline: none;
    line-height: 1.5;
    transition: border-color 0.15s;
  }

  #input:focus { border-color: #2a2a2a; }
  #input::placeholder { color: var(--text-dim); }

  #input-wrap { position: relative; }

  #send-btn {
    position: absolute;
    right: 6px;
    bottom: 6px;
    background: transparent;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
    font-size: 13px;
    padding: 2px 4px;
    transition: color 0.15s;
    line-height: 1;
  }

  #send-btn:hover { color: var(--accent); }
  #send-btn:disabled { opacity: 0.2; cursor: not-allowed; }

  #hint {
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 9px;
    color: var(--text-dim);
    letter-spacing: 0.08em;
    margin-top: 5px;
    text-align: center;
  }
</style>
</head>
<body>

<div id="ctx-bar">no file open</div>

<div id="messages">
  <div class="msg-row system-row">
    <span class="system-note">kamil · local · /council for deliberation</span>
  </div>
</div>

<div id="input-area">
  <div id="input-wrap">
    <textarea id="input" placeholder="ask kamil..." rows="1"></textarea>
    <button id="send-btn" onclick="send()">↑</button>
  </div>
  <div id="hint">/council prefix for deliberation · shift+enter for newline</div>
</div>

<script>
const vscode = acquireVsCodeApi();
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const ctxBar = document.getElementById('ctx-bar');
const sendBtn = document.getElementById('send-btn');

let currentContent = null;
let currentCursor = null;

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
});

inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

function setDisabled(val) {
  sendBtn.disabled = val;
  inputEl.disabled = val;
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendSystem(text) {
  const row = document.createElement('div');
  row.className = 'msg-row system-row';
  row.innerHTML = '<span class="system-note">' + text + '</span>';
  messagesEl.appendChild(row);
  scrollBottom();
}

function appendUser(text) {
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML = '<div class="bubble user">' + escapeHtml(text) + '</div>';
  messagesEl.appendChild(row);
  scrollBottom();
}

function appendAssistant(isCouncil) {
  const row = document.createElement('div');
  row.className = 'msg-row assistant';
  const bubble = document.createElement('div');
  bubble.className = 'bubble assistant' + (isCouncil ? ' council' : '');
  const tag = document.createElement('span');
  tag.className = 'role-tag' + (isCouncil ? ' council' : '');
  tag.textContent = isCouncil ? 'COUNCIL' : 'KAMIL';
  bubble.appendChild(tag);
  const content = document.createElement('span');
  bubble.appendChild(content);
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  scrollBottom();
  return content;
}

function escapeHtml(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  setDisabled(true);
  appendUser(text);
  currentContent = appendAssistant(false);
  currentCursor = document.createElement('span');
  currentCursor.className = 'cursor';
  currentContent.parentElement.appendChild(currentCursor);
  vscode.postMessage({ type: 'chat', text, mode: 'action' });
}

window.addEventListener('message', e => {
  const msg = e.data;

  if (msg.type === 'prefill') {
    inputEl.value = msg.text;
    inputEl.focus();

  } else if (msg.type === 'context') {
    if (msg.file) {
      const short = msg.file.split(/[\\\\/]/).pop();
      const proj = msg.project ? msg.project + ' · ' : '';
      ctxBar.textContent = msg.selection
        ? proj + short + ' · selection'
        : proj + short;
      ctxBar.className = 'active';
    } else {
      ctxBar.textContent = 'no file open';
      ctxBar.className = '';
    }

  } else if (msg.type === 'stream_start') {
    if (currentContent) currentContent.textContent = '';
    if (currentCursor && currentContent) currentContent.parentElement.appendChild(currentCursor);

  } else if (msg.type === 'stream_chunk') {
    if (currentContent) {
      if (currentCursor) currentCursor.remove();
      currentContent.textContent += msg.chunk;
      if (currentCursor) currentContent.parentElement.appendChild(currentCursor);
      scrollBottom();
    }

  } else if (msg.type === 'stream_end') {
    if (currentCursor) currentCursor.remove();
    setDisabled(false);
    inputEl.focus();

  } else if (msg.type === 'response') {
    if (currentCursor) currentCursor.remove();
    if (msg.mode === 'council' && msg.data && msg.data.analyst) {
      if (currentContent) currentContent.parentElement.parentElement.remove();
      const block = document.createElement('div');
      block.className = 'council-block';
      block.innerHTML =
        '<div class="c-label analyst">ANALYST</div>' +
        '<div class="c-body analyst">' + escapeHtml(msg.data.analyst || '') + '</div>' +
        '<div class="c-label advocate">DEVIL\'S ADVOCATE</div>' +
        '<div class="c-body advocate">' + escapeHtml(msg.data.advocate || '') + '</div>' +
        '<div class="c-label synth">SYNTHESIZER</div>' +
        '<div class="c-body synth">' + escapeHtml(msg.data.synthesizer || '') + '</div>';
      messagesEl.appendChild(block);
      scrollBottom();
    } else if (msg.data && msg.data.response) {
      if (currentContent) currentContent.textContent = msg.data.response;
    } else if (msg.data && msg.data.error) {
      if (currentContent) currentContent.textContent = 'error: ' + msg.data.error;
    }
    setDisabled(false);
    inputEl.focus();

  } else if (msg.type === 'error') {
    if (currentCursor) currentCursor.remove();
    if (currentContent) currentContent.textContent = 'error: ' + msg.message;
    setDisabled(false);
  }
});

setInterval(() => vscode.postMessage({ type: 'getContext' }), 2000);
vscode.postMessage({ type: 'getContext' });
</script>
</body>
</html>`
export function deactivate() {}