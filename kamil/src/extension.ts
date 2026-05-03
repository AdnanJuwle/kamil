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

    constructor(private readonly _extensionUri: vscode.Uri) {}

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

        webviewView.webview.onDidReceiveMessage(async (data) => {
            if (data.type === 'getContext') {
                const ctx = this._getEditorContext();
                webviewView.webview.postMessage({ type: 'context', ...ctx });

                if (ctx.project) {
                    try {
                        await fetch(`${KAMIL_URL}/select_project`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ project_name: ctx.project })
                        });
                    } catch {}
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
                    // Both 'chat' and 'action' modes stream through /chat_actions
                    try {
                        const res = await fetch(`${KAMIL_URL}/chat_actions`, {
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
                                } catch {}
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
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d0d0d;
    color: #e0e0e0;
    font-family: var(--vscode-font-family);
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #context-bar {
    padding: 6px 10px;
    background: #111;
    border-bottom: 1px solid #1e1e1e;
    font-size: 11px;
    color: #444;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #context-bar.active { color: #4a9eff; }
  #messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .message { line-height: 1.5; flex-shrink: 0; white-space: pre-wrap; word-break: break-word; }
  .message.user { color: #888; }
  .message.user::before {
    content: "YOU";
    display: block;
    font-size: 9px;
    letter-spacing: 2px;
    color: #333;
    margin-bottom: 4px;
  }
  .message.assistant { color: #e0e0e0; }
  .message.assistant::before {
    content: "KAMIL";
    display: block;
    font-size: 9px;
    letter-spacing: 2px;
    color: #4a9eff;
    margin-bottom: 4px;
  }
  .system-msg {
    font-size: 10px;
    color: #333;
    text-align: center;
    letter-spacing: 1px;
    flex-shrink: 0;
  }
  #input-area {
    padding: 8px;
    border-top: 1px solid #1e1e1e;
  }
  #input {
    width: 100%;
    background: #111;
    border: 1px solid #222;
    border-radius: 4px;
    color: #e0e0e0;
    padding: 8px;
    font-size: 12px;
    font-family: inherit;
    resize: none;
    min-height: 60px;
    max-height: 150px;
    outline: none;
    margin-bottom: 6px;
  }
  #input:focus { border-color: #333; }
  #buttons { display: flex; gap: 6px; }
  .btn {
    flex: 1;
    padding: 6px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
    letter-spacing: 1px;
    border: 1px solid;
    transition: opacity 0.15s;
  }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #send-btn { background: #1a1a2e; border-color: #2a2a4a; color: #4a9eff; }
  #action-btn { background: #0d1a0d; border-color: #2a4a2a; color: #4aff4a; }
  #council-btn { background: #1a0a2e; border-color: #4a2a8a; color: #9a6aff; }
  .cursor {
    display: inline-block;
    width: 2px;
    height: 12px;
    background: #4a9eff;
    margin-left: 1px;
    animation: blink 0.8s infinite;
    vertical-align: middle;
  }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  #messages::-webkit-scrollbar { width: 3px; }
  #messages::-webkit-scrollbar-thumb { background: #222; border-radius: 2px; }
  .council-block { border: 1px solid #2a2a2a; border-radius: 4px; overflow: hidden; flex-shrink: 0; }
  .council-label { padding: 4px 8px; font-size: 9px; letter-spacing: 2px; border-bottom: 1px solid #1a1a1a; }
  .council-body { padding: 8px; max-height: 150px; overflow-y: auto; font-size: 12px; white-space: pre-wrap; }
</style>
</head>
<body>
<div id="context-bar">No file open</div>
<div id="messages">
  <div class="system-msg">KAMIL · LOCAL · OFFLINE</div>
</div>
<div id="input-area">
  <textarea id="input" placeholder="Ask Kamil..."></textarea>
  <div id="buttons">
    <button class="btn" id="send-btn" onclick="send('chat')">SEND</button>
    <button class="btn" id="action-btn" onclick="send('action')">ACTION</button>
    <button class="btn" id="council-btn" onclick="send('council')">COUNCIL</button>
  </div>
</div>
<script>
  const vscode = acquireVsCodeApi();
  const messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('input');
  const contextBar = document.getElementById('context-bar');
  const btns = document.querySelectorAll('.btn');
  let currentDiv = null;
  let currentCursor = null;

  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send('chat');
    }
  });

  function setDisabled(val) { btns.forEach(b => b.disabled = val); }

  function appendMsg(role, text) {
    const div = document.createElement('div');
    div.className = 'message ' + role;
    div.textContent = text || '';
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function appendSystem(text) {
    const div = document.createElement('div');
    div.className = 'system-msg';
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function send(mode) {
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = '';
    setDisabled(true);
    appendMsg('user', text);
    currentDiv = appendMsg('assistant', '');
    currentCursor = document.createElement('span');
    currentCursor.className = 'cursor';
    currentDiv.appendChild(currentCursor);
    vscode.postMessage({ type: 'chat', text, mode: mode === 'council' ? 'council' : 'action' });
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
        contextBar.textContent = msg.selection ? proj + short + ' · selection' : proj + short;
        contextBar.className = 'active';
      } else {
        contextBar.textContent = 'No file open';
        contextBar.className = '';
      }
    } else if (msg.type === 'stream_start') {
      if (currentDiv) currentDiv.textContent = '';
      if (currentCursor) currentDiv.appendChild(currentCursor);
    } else if (msg.type === 'stream_chunk') {
      if (currentDiv) {
        if (currentCursor) currentCursor.remove();
        currentDiv.textContent += msg.chunk;
        currentDiv.appendChild(currentCursor);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    } else if (msg.type === 'stream_end') {
      if (currentCursor) currentCursor.remove();
      setDisabled(false);
      inputEl.focus();
    } else if (msg.type === 'response') {
      if (currentCursor) currentCursor.remove();
      if (msg.mode === 'council' && msg.data && msg.data.analyst) {
        if (currentDiv) currentDiv.textContent = '[Council]';
        const block = document.createElement('div');
        block.className = 'council-block';
        block.innerHTML =
          '<div class="council-label" style="color:#4aff4a;background:#0a120a">ANALYST</div>' +
          '<div class="council-body" style="color:#c0d0c0;background:#0a120a">' + (msg.data.analyst || '') + '</div>' +
          '<div class="council-label" style="color:#ff6a4a;background:#120a0a">DEVIL&#39;S ADVOCATE</div>' +
          '<div class="council-body" style="color:#d0c0c0;background:#120a0a">' + (msg.data.advocate || '') + '</div>' +
          '<div class="council-label" style="color:#9a6aff;background:#0a0a12">SYNTHESIZER</div>' +
          '<div class="council-body" style="color:#e0e0e0;background:#0a0a12">' + (msg.data.synthesizer || '') + '</div>';
        messagesEl.appendChild(block);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else if (msg.mode === 'action' && msg.data && msg.data.response) {
        if (currentDiv) currentDiv.textContent = msg.data.response;
      } else if (msg.data && msg.data.error) {
        if (currentDiv) currentDiv.textContent = 'Error: ' + msg.data.error;
      }
      setDisabled(false);
      inputEl.focus();
    } else if (msg.type === 'error') {
      if (currentCursor) currentCursor.remove();
      if (currentDiv) currentDiv.textContent = 'Error: ' + msg.message;
      setDisabled(false);
    }
  });

  setInterval(() => vscode.postMessage({ type: 'getContext' }), 2000);
  vscode.postMessage({ type: 'getContext' });
</script>
</body>
</html>`;
    }
}

export function deactivate() {}