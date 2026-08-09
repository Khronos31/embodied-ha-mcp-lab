document.addEventListener('DOMContentLoaded', () => {
  // DOM References
  const serverSelect = document.getElementById('server-select');
  const toolSelect = document.getElementById('tool-select');
  const toolDetailsSection = document.getElementById('tool-details-section');
  const toolDescriptionText = document.getElementById('tool-description-text');
  const toolSchemaDisplay = document.getElementById('tool-schema-display');
  const sideEffectWarning = document.getElementById('side-effect-warning');
  const toolInput = document.getElementById('tool-input');
  const sendToolBtn = document.getElementById('send-tool-btn');
  const resetStateBtn = document.getElementById('reset-state-btn');
  const stateSummaryGrid = document.getElementById('state-summary-grid');
  const statusMessage = document.getElementById('status-message');

  // Result Elements
  const resultMetaGrid = document.getElementById('result-meta-grid');
  const resExitCode = document.getElementById('res-exit-code');
  const resSignal = document.getElementById('res-signal');
  const resTimedOut = document.getElementById('res-timed-out');
  const resElapsedMs = document.getElementById('res-elapsed-ms');
  const resResponseId = document.getElementById('res-response-id');
  const resInputClass = document.getElementById('res-input-class');
  const resLineBreaks = document.getElementById('res-line-breaks');
  const resRequestLayer = document.getElementById('res-request-layer');
  const resTruncated = document.getElementById('res-truncated');
  const resStdoutRaw = document.getElementById('res-stdout-raw');
  const resStderrRaw = document.getElementById('res-stderr-raw');
  const resStateChanges = document.getElementById('res-state-changes');
  const resFullEnvelope = document.getElementById('res-full-envelope');

  // Local state & race condition tracking
  let currentTools = [];
  let activeToolsFetchServer = null;

  // API URL Builder using injected window.INGRESS_PATH
  function getApiUrl(path) {
    const base = (window.INGRESS_PATH || '').replace(/\/+$/, '');
    const cleanPath = path.startsWith('/') ? path : '/' + path;
    return base + cleanPath;
  }

  // Format bytes helper
  function formatBytes(bytes) {
    if (bytes === undefined || bytes === null || isNaN(bytes)) return '-';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }

  // Status message helper
  function showStatus(message, isError = false) {
    statusMessage.textContent = message;
    statusMessage.className = 'status-message ' + (isError ? 'status-error' : 'status-success');
  }

  function clearStatus() {
    statusMessage.textContent = '';
    statusMessage.className = 'status-message hidden';
  }

  // 1. Load Compact State Summary
  async function loadStateSummary() {
    try {
      const response = await fetch(getApiUrl('/api/state'));
      if (!response.ok) {
        const errText = await response.text();
        showStatus(`状態サマリーの取得失敗 [HTTP ${response.status} ${response.statusText}]: ${errText}`, true);
        return;
      }
      const state = await response.json();
      renderStateSummary(state);
    } catch (err) {
      showStatus(`状態サマリー通信エラー: ${err.message}`, true);
    }
  }

  function renderStateSummary(state) {
    stateSummaryGrid.textContent = '';

    const rawHead = state.state_head || state.short_head || state.head_short || state.head || '';
    const shortHead = rawHead ? String(rawHead).slice(0, 12) : '-';

    const items = [
      {
        label: '世代ブランチ (Generation Branch)',
        value: state.state_generation_branch || state.generation_branch || state.gen_branch || '-'
      },
      {
        label: 'HEAD (Short)',
        value: shortHead
      },
      {
        label: 'ワークツリーサイズ (Worktree Bytes)',
        value: formatBytes(state.worktree_bytes)
      },
      {
        label: 'リポジトリサイズ (Repository Bytes)',
        value: formatBytes(state.repository_bytes)
      },
      {
        label: '世代ブランチ数 (Generation Branch Count)',
        value: state.generation_branch_count ?? '-'
      },
      {
        label: 'キュー深度 / 現在の処理 (Queue / Active Operation)',
        value: `${state.queue_depth ?? 0} / ${state.active_operation_id || state.current_operation || 'idle'}`
      }
    ];

    items.forEach(item => {
      const div = document.createElement('div');
      div.className = 'summary-item';

      const labelSpan = document.createElement('span');
      labelSpan.className = 'summary-label';
      labelSpan.textContent = item.label;

      const valueSpan = document.createElement('span');
      valueSpan.className = 'summary-value';
      valueSpan.textContent = String(item.value);

      div.appendChild(labelSpan);
      div.appendChild(valueSpan);
      stateSummaryGrid.appendChild(div);
    });
  }

  // 2. Fetch Servers
  async function loadServers() {
    try {
      const response = await fetch(getApiUrl('/api/servers'));
      if (!response.ok) {
        const errText = await response.text();
        showStatus(`サーバー一覧の取得失敗 [HTTP ${response.status} ${response.statusText}]: ${errText}`, true);
        return;
      }
      const data = await response.json();
      populateServers(data.servers || []);
    } catch (err) {
      showStatus(`サーバー一覧通信エラー: ${err.message}`, true);
    }
  }

  function populateServers(servers) {
    serverSelect.textContent = '';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = '-- サーバーを選択してください --';
    serverSelect.appendChild(defaultOpt);

    servers.forEach(srv => {
      const name = typeof srv === 'string' ? srv : (srv.name || srv.id || String(srv));
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      serverSelect.appendChild(opt);
    });
  }

  // 3. Server Selection -> Live-fetch Tools (with race condition prevention)
  serverSelect.addEventListener('change', async () => {
    const selectedServer = serverSelect.value;
    activeToolsFetchServer = selectedServer;

    toolSelect.textContent = '';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = '-- ツールを選択してください --';
    toolSelect.appendChild(defaultOpt);

    currentTools = [];
    hideToolDetails();

    if (!selectedServer) return;

    try {
      const response = await fetch(getApiUrl(`/api/servers/${encodeURIComponent(selectedServer)}/tools`));
      
      // Prevent stale response from populating dropdown if server changed during fetch
      if (serverSelect.value !== selectedServer || activeToolsFetchServer !== selectedServer) {
        return;
      }

      if (!response.ok) {
        const errText = await response.text();
        showStatus(`ツール一覧の取得失敗 [HTTP ${response.status} ${response.statusText}]: ${errText}`, true);
        return;
      }
      const data = await response.json();
      
      // Re-check after async JSON parsing
      if (serverSelect.value !== selectedServer || activeToolsFetchServer !== selectedServer) {
        return;
      }

      currentTools = data.tools || [];
      currentTools.forEach((tool, idx) => {
        const opt = document.createElement('option');
        opt.value = tool.name || String(idx);
        opt.textContent = tool.name || `Tool ${idx + 1}`;
        toolSelect.appendChild(opt);
      });
    } catch (err) {
      if (serverSelect.value === selectedServer) {
        showStatus(`ツール一覧通信エラー: ${err.message}`, true);
      }
    }
  });

  // 4. One-line Compact JSON Convenience Template Generator
  function generateDefaultValue(schema) {
    if (!schema || typeof schema !== 'object') {
      return null;
    }

    if ('default' in schema && schema.default !== undefined) {
      return schema.default;
    }

    if (Array.isArray(schema.anyOf) && schema.anyOf.length > 0) {
      const nonNull = schema.anyOf.find(s => s && s.type !== 'null');
      return generateDefaultValue(nonNull || schema.anyOf[0]);
    }

    if (Array.isArray(schema.oneOf) && schema.oneOf.length > 0) {
      const nonNull = schema.oneOf.find(s => s && s.type !== 'null');
      return generateDefaultValue(nonNull || schema.oneOf[0]);
    }

    let type = schema.type;
    if (Array.isArray(type)) {
      const nonNullType = type.find(t => t !== 'null');
      type = nonNullType || type[0];
    }

    switch (type) {
      case 'string':
        return '';
      case 'number':
      case 'integer':
        return 0;
      case 'boolean':
        return false;
      case 'array':
        return [];
      case 'object':
        return {};
      case 'null':
        return null;
      default:
        return null;
    }
  }

  function generateTemplate(inputSchema) {
    if (!inputSchema || typeof inputSchema !== 'object') {
      return '{}';
    }

    const properties = inputSchema.properties;
    if (!properties || typeof properties !== 'object' || Object.keys(properties).length === 0) {
      return '{}';
    }

    const templateObj = {};
    for (const key of Object.keys(properties)) {
      templateObj[key] = generateDefaultValue(properties[key]);
    }

    return JSON.stringify(templateObj);
  }

  // 5. Explicit Side-effect & Privacy Observation Warning logic
  function checkSideEffectWarning(serverName, tool) {
    const srv = (serverName || '').toLowerCase().trim();
    const toolName = (tool.name || '').toLowerCase().trim();
    const fullPath = `${srv}/${toolName}`;

    let isSideEffect = false;
    let isPrivacyObs = false;

    // Explicit known side-effect categories
    if (
      srv === 'hacontrol' || fullPath.startsWith('hacontrol/') ||
      fullPath === 'audio/speak' || (srv === 'audio' && toolName === 'speak') ||
      fullPath === 'audio/use_device_speaker' || (srv === 'audio' && toolName === 'use_device_speaker') ||
      srv === 'song' || fullPath.startsWith('song/') ||
      fullPath === 'http/http_post' || (srv === 'http' && toolName === 'http_post') ||
      fullPath === 'lounge/enqueue_lounge_post' || (srv === 'lounge' && toolName === 'enqueue_lounge_post') ||
      srv === 'game' || fullPath.startsWith('game/')
    ) {
      isSideEffect = true;
    }

    // Household-observation camera / audio tools privacy check
    if (
      srv === 'camera' || srv === 'observation' || srv === 'household_observation' || srv === 'vision' ||
      toolName.includes('camera') || toolName.includes('video') || toolName.includes('stream') ||
      toolName.includes('capture') || toolName.includes('snapshot') || toolName.includes('record') ||
      toolName.includes('mic') || toolName.includes('listen') || toolName.includes('audio') ||
      toolName.includes('speak') || toolName.includes('speaker')
    ) {
      isPrivacyObs = true;
    }

    let warningText = '';
    if (isSideEffect && isPrivacyObs) {
      warningText = `【副作用・プライバシー注意】 このツール (${serverName}/${tool.name}) は外部変更を伴い、カメラ/音声などの室内観測・出力を行います。`;
    } else if (isSideEffect) {
      warningText = `【副作用注意】 このツール (${serverName}/${tool.name}) は状態変更や外部データ送信を発生させます。`;
    } else if (isPrivacyObs) {
      warningText = `【環境計測/プライバシー注意】 このツール (${serverName}/${tool.name}) はカメラ/音声などの室内センシング・観測を行います。`;
    }

    if (warningText) {
      sideEffectWarning.textContent = warningText;
      sideEffectWarning.classList.remove('hidden');
    } else {
      sideEffectWarning.textContent = '';
      sideEffectWarning.classList.add('hidden');
    }
  }

  function hideToolDetails() {
    toolDetailsSection.classList.add('hidden');
    toolDescriptionText.textContent = '';
    toolSchemaDisplay.textContent = '';
    sideEffectWarning.textContent = '';
    sideEffectWarning.classList.add('hidden');
  }

  // 6. Tool Selection Listener
  toolSelect.addEventListener('change', () => {
    const selectedToolName = toolSelect.value;
    if (!selectedToolName) {
      hideToolDetails();
      return;
    }

    const selectedTool = currentTools.find(t => t.name === selectedToolName) || currentTools[parseInt(selectedToolName, 10)];
    if (!selectedTool) {
      hideToolDetails();
      return;
    }

    toolDescriptionText.textContent = selectedTool.description || '(説明なし)';

    const schemaObj = selectedTool.inputSchema || {};
    toolSchemaDisplay.textContent = JSON.stringify(schemaObj, null, 2);

    toolInput.value = generateTemplate(schemaObj);

    checkSideEffectWarning(serverSelect.value, selectedTool);

    toolDetailsSection.classList.remove('hidden');
  });

  // 7. Send Button Handler
  sendToolBtn.addEventListener('click', async () => {
    const server = serverSelect.value;
    const tool = toolSelect.value;

    if (!server || !tool) {
      showStatus('送信エラー: サーバーとツールを選択してください。', true);
      return;
    }

    clearStatus();
    sendToolBtn.disabled = true;

    const payload = toolInput.value;
    const url = getApiUrl(`/api/servers/${encodeURIComponent(server)}/tools/${encodeURIComponent(tool)}/call`);

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'text/plain;charset=utf-8'
        },
        body: payload
      });

      if (!response.ok) {
        const errText = await response.text();
        showStatus(`ツール呼び出し失敗 [HTTP ${response.status} ${response.statusText}]: ${errText}`, true);
        renderErrorEnvelope(response.status, response.statusText, errText);
      } else {
        const envelope = await response.json();
        renderResultEnvelope(envelope);
        showStatus('ツール呼び出しが完了しました。', false);
      }
    } catch (err) {
      showStatus(`ツール呼び出し通信エラー: ${err.message}`, true);
      renderErrorEnvelope('NET_ERR', 'Network Error', err.message);
    } finally {
      sendToolBtn.disabled = false;
    }
  });

  function renderResultEnvelope(envelope) {
    resultMetaGrid.classList.remove('hidden');

    resExitCode.textContent = envelope.exit_code !== undefined ? String(envelope.exit_code) : '-';
    resSignal.textContent = envelope.signal !== undefined ? String(envelope.signal) : '-';
    resTimedOut.textContent = envelope.timed_out !== undefined ? String(envelope.timed_out) : '-';
    resElapsedMs.textContent = envelope.elapsed_ms !== undefined ? String(envelope.elapsed_ms) : '-';

    // Render literal boolean false if response_id_observed is false
    let resIdObserved = envelope.response_id_observed;
    if (resIdObserved === undefined && envelope.response_id !== undefined) {
      resIdObserved = envelope.response_id;
    }
    resResponseId.textContent = resIdObserved !== undefined ? String(resIdObserved) : '-';

    resInputClass.textContent = envelope.input_class || '-';
    resLineBreaks.textContent = envelope.input_line_breaks !== undefined ? String(envelope.input_line_breaks) : '-';
    resRequestLayer.textContent = envelope.request_layer || '-';
    resTruncated.textContent = envelope.truncated !== undefined ? String(envelope.truncated) : '-';

    resStdoutRaw.textContent = envelope.stdout_raw !== undefined ? envelope.stdout_raw : '';
    resStderrRaw.textContent = envelope.stderr_raw !== undefined ? envelope.stderr_raw : '';

    // State Commits & Changes block formatting
    if (envelope.old_branch !== undefined || envelope.new_branch !== undefined || envelope.old_head !== undefined || envelope.new_head !== undefined) {
      // Reset response specific format
      const lines = [
        `old_branch: ${envelope.old_branch || '-'}`,
        `new_branch: ${envelope.new_branch || '-'}`,
        `old_head:   ${envelope.old_head ? String(envelope.old_head).slice(0, 12) : '-'}`,
        `new_head:   ${envelope.new_head ? String(envelope.new_head).slice(0, 12) : '-'}`
      ];
      if (envelope.commits !== undefined || envelope.state_commits !== undefined) {
        const commits = envelope.commits || envelope.state_commits;
        lines.push(`commits:\n${typeof commits === 'object' ? JSON.stringify(commits, null, 2) : commits}`);
      }
      resStateChanges.textContent = lines.join('\n');
    } else {
      // Normal tool call response format
      const parts = [];
      if (envelope.state_commit_before !== undefined) {
        parts.push(`state_commit_before: ${String(envelope.state_commit_before).slice(0, 12)}`);
      }
      if (envelope.state_commit_after !== undefined) {
        parts.push(`state_commit_after:  ${String(envelope.state_commit_after).slice(0, 12)}`);
      }
      if (envelope.state_changes !== undefined) {
        const sc = envelope.state_changes;
        parts.push(`state_changes:\n${typeof sc === 'object' ? JSON.stringify(sc, null, 2) : sc}`);
      } else if (envelope.state_commits !== undefined) {
        const sc = envelope.state_commits;
        parts.push(`state_commits:\n${typeof sc === 'object' ? JSON.stringify(sc, null, 2) : sc}`);
      }
      
      if (parts.length > 0) {
        resStateChanges.textContent = parts.join('\n');
      } else {
        const fallbackData = envelope.commits || envelope.changes || envelope.state;
        resStateChanges.textContent = typeof fallbackData === 'object'
          ? JSON.stringify(fallbackData, null, 2)
          : (fallbackData !== undefined ? String(fallbackData) : '-');
      }
    }

    resFullEnvelope.textContent = JSON.stringify(envelope, null, 2);
  }

  function renderErrorEnvelope(status, statusText, errBody) {
    resultMetaGrid.classList.remove('hidden');

    resExitCode.textContent = String(status);
    resSignal.textContent = '-';
    resTimedOut.textContent = '-';
    resElapsedMs.textContent = '-';
    resResponseId.textContent = '-';
    resInputClass.textContent = '-';
    resLineBreaks.textContent = '-';
    resRequestLayer.textContent = '-';
    resTruncated.textContent = '-';

    resStdoutRaw.textContent = '';
    resStderrRaw.textContent = `[HTTP ERROR ${status} ${statusText}]\n${errBody}`;
    resStateChanges.textContent = '';
    resFullEnvelope.textContent = JSON.stringify({ status, statusText, error: errBody }, null, 2);
  }

  // 8. Reset Lab State Button Handler
  resetStateBtn.addEventListener('click', async () => {
    const confirmMessage = '新しいブランチが初期状態から開始されますが、古い状態および入力された情報はローカルGit履歴に残ります。リセットを実行しますか？';
    if (!window.confirm(confirmMessage)) {
      return;
    }

    resetStateBtn.disabled = true;
    clearStatus();

    try {
      const response = await fetch(getApiUrl('/api/state/reset'), {
        method: 'POST',
        body: ''
      });

      if (!response.ok) {
        const errText = await response.text();
        showStatus(`リセット失敗 [HTTP ${response.status} ${response.statusText}]: ${errText}`, true);
        renderErrorEnvelope(response.status, response.statusText, errText);
      } else {
        const resetData = await response.json();
        showStatus('ラボ状態をリセットしました。古い状態と新しいブランチ情報をご確認ください。', false);
        renderResultEnvelope(resetData);
        await loadStateSummary();
      }
    } catch (err) {
      showStatus(`リセット通信エラー: ${err.message}`, true);
    } finally {
      resetStateBtn.disabled = false;
    }
  });

  // Initial Load
  loadStateSummary();
  loadServers();
});
