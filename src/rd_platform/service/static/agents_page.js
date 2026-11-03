'use strict';
(function () {
  const ROLE_ORDER = ['ARCHITECT', 'CODER', 'REVIEWER', 'TESTER'];
  const ROLE_LABELS = { ARCHITECT: '架构师', CODER: '开发者', REVIEWER: '审查者', TESTER: '测试者' };
  const ROLE_HINTS = {
    ARCHITECT: '负责架构与任务拆解',
    CODER: '负责代码与测试实现',
    REVIEWER: '负责代码审查与修正意见',
    TESTER: '固定通过 Docker 沙箱验证，无模型调用',
  };
  const MESSAGES = {
    loadingProjects: '正在加载项目列表…',
    empty: '暂无可配置项目，请先在项目列表新建项目。',
    loadingConfig: '正在加载角色配置…',
    ready: '配置已加载。',
    saving: '正在保存角色配置…',
    saved: '角色配置已保存，新运行将使用此版本。',
    conflict: '配置版本冲突：保存被拒绝，草稿已保留。请重新加载最新配置；重新加载会放弃当前草稿。',
    reloadDiscard: '重新加载将放弃当前未保存的修改，是否继续？',
    network: '网络错误：保存结果未知，草稿已保留。请重新加载确认服务端结果。',
    unknownResult: '保存结果未知，草稿已保留。请重新加载确认服务端结果。',
    forbidden: '当前请求被拒绝（403）。',
    validation: '提交的配置未通过校验，请检查输入后重试。',
    notFound: '项目不存在或已被移除。',
    server: '服务端错误，请稍后重试。',
    loadError: '角色配置加载失败。',
    projectError: '项目列表加载失败。',
    invalidSnapshot: '角色配置响应无效，未能加载。',
  };

  const dom = {};
  const state = {
    generation: 0,
    phase: 'loading-projects',
    projectId: null,
    revision: null,
    snapshot: null,
    dirty: false,
    abort: null,
    saveAbort: null,
  };

  function $(id) { return document.getElementById(id); }

  function setStatus(text) { if (dom.status) dom.status.textContent = text; }
  function setError(text) { if (dom.error) dom.error.textContent = text || ''; }

  function snapshotMatches(projectId) {
    return !!(state.snapshot && state.projectId && state.projectId === projectId
      && Number.isInteger(state.revision) && state.revision >= 0);
  }

  function setControls() {
    const busy = state.phase === 'saving';
    const interactive = state.phase === 'ready' || state.phase === 'conflict' || state.phase === 'error' || state.phase === 'empty';
    dom.project.disabled = busy || !interactive;
    dom.refresh.disabled = busy || state.phase === 'loading-projects' || state.phase === 'loading-config';
    dom.save.disabled = !(state.phase === 'ready') || !snapshotMatches(state.projectId);
    for (const control of dom.cards.querySelectorAll('select,input,textarea')) {
      control.disabled = state.phase !== 'ready' || control.dataset.fixed === 'true';
    }
  }

  function option(value, label) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    return opt;
  }

  function renderProjects(projects) {
    dom.project.replaceChildren();
    dom.projectOptions = Array.isArray(projects)
      ? projects.filter(p => p && typeof p.id === 'string' && typeof p.name === 'string')
      : [];
    if (dom.projectOptions.length === 0) {
      dom.project.replaceChildren(option('', '暂无项目'));
      state.phase = 'empty';
      state.snapshot = null;
      state.revision = null;
      state.projectId = null;
      dom.cards.replaceChildren();
      renderEmpty(MESSAGES.empty);
      renderRelease();
      setStatus('');
      setError('');
      setControls();
      return;
    }
    for (const project of dom.projectOptions) dom.project.append(option(project.id, project.name));
    dom.project.value = dom.projectOptions[0].id;
    setControls();
    loadConfig(dom.projectOptions[0].id);
  }

  function renderEmpty(message) {
    const box = document.createElement('div');
    box.className = 'agents-empty';
    box.textContent = message;
    dom.cards.replaceChildren(box);
  }

  function loadProjects() {
    cancelPending();
    cancelSave();
    state.phase = 'loading-projects';
    state.snapshot = null;
    state.revision = null;
    state.projectId = null;
    state.dirty = false;
    setControls();
    setError('');
    setStatus(MESSAGES.loadingProjects);
    renderRelease();
    const generation = ++state.generation;
    fetch('/v1/projects', { headers: { Accept: 'application/json' } })
      .then(response => {
        if (generation !== state.generation) return null;
        if (!response.ok) throw new Error('projects');
        return response.json();
      })
      .then(data => {
        if (data === null || generation !== state.generation) return;
        renderProjects(data && Array.isArray(data.projects) ? data.projects : []);
      })
      .catch(() => {
        if (generation !== state.generation) return;
        state.phase = 'error';
        state.snapshot = null;
        state.revision = null;
        renderEmpty(MESSAGES.projectError);
        renderRelease();
        setStatus('');
        setError(MESSAGES.projectError);
        setControls();
      });
  }

  function cancelPending() {
    if (state.abort) {
      try { state.abort.abort(); } catch (_) { /* ignore */ }
      state.abort = null;
    }
  }

  function cancelSave() {
    if (state.saveAbort) {
      try { state.saveAbort.abort(); } catch (_) { /* ignore */ }
      state.saveAbort = null;
    }
  }

  function loadConfig(projectId) {
    cancelPending();
    state.projectId = projectId;
    state.snapshot = null;
    state.revision = null;
    state.dirty = false;
    state.phase = 'loading-config';
    setControls();
    setError('');
    setStatus(MESSAGES.loadingConfig);
    renderRelease();
    dom.cards.replaceChildren();
    const generation = ++state.generation;
    const controller = new AbortController();
    state.abort = controller;
    fetch('/v1/projects/' + encodeURIComponent(projectId) + '/agents/config', { headers: { Accept: 'application/json' }, signal: controller.signal })
      .then(response => {
        if (generation !== state.generation) return null;
        if (response.status === 404) throw new Error('notfound');
        if (!response.ok) throw new Error('config');
        return response.json();
      })
      .then(data => {
        if (data === null || generation !== state.generation) return;
        applySnapshot(projectId, data);
      })
      .catch(error => {
        if (generation !== state.generation) return;
        if (error && error.name === 'AbortError') return;
        state.phase = 'error';
        state.snapshot = null;
        state.revision = null;
        renderEmpty(error && error.message === 'notfound' ? MESSAGES.notFound : MESSAGES.loadError);
        renderRelease();
        setStatus('');
        setError(error && error.message === 'notfound' ? MESSAGES.notFound : MESSAGES.loadError);
        setControls();
      });
  }

  function roleDraft(role, spec) {
    const source = spec && typeof spec === 'object' ? spec : {};
    if (role === 'TESTER') {
      return {
        backend: 'sandbox',
        model: null,
        responsibility: typeof source.responsibility === 'string' ? source.responsibility : '',
        tools: source.tools,
        credential_ref: source.credential_ref,
      };
    }
    const backend = source.backend === 'llm' ? 'llm' : 'codex';
    const model = typeof source.model === 'string' ? source.model : '';
    return {
      backend,
      model,
      responsibility: typeof source.responsibility === 'string' ? source.responsibility : '',
      tools: source.tools,
      credential_ref: source.credential_ref,
    };
  }

  function applySnapshot(projectId, data) {
    if (!data || typeof data !== 'object' || !Number.isInteger(data.revision) || data.revision < 0
        || !data.agents || typeof data.agents !== 'object') {
      state.phase = 'error';
      state.snapshot = null;
      state.revision = null;
      renderEmpty(MESSAGES.invalidSnapshot);
      renderRelease();
      setStatus('');
      setError(MESSAGES.invalidSnapshot);
      setControls();
      return;
    }
    const agents = data.agents;
    const snapshot = { revision: data.revision, agents: {} };
    for (const role of ROLE_ORDER) snapshot.agents[role] = roleDraft(role, agents[role]);
    state.projectId = projectId;
    state.revision = snapshot.revision;
    state.snapshot = snapshot;
    state.dirty = false;
    state.phase = 'ready';
    dom.project.value = projectId;
    renderCards();
    renderRelease();
    setStatus(MESSAGES.ready);
    setError('');
    setControls();
  }

  function renderRelease() {
    if (!dom.release) return;
    if (state.revision === null || state.revision === undefined) {
      dom.release.textContent = '';
    } else if (state.revision === 0) {
      dom.release.textContent = '当前为默认模板，编辑并保存后才会用于新运行。';
    } else {
      dom.release.textContent = '已保存 · 配置版本 ' + String(state.revision) + '。新运行将使用此版本。';
    }
  }

  function renderCards() {
    const fragment = document.createDocumentFragment();
    for (const role of ROLE_ORDER) {
      const spec = state.snapshot.agents[role];
      const card = document.createElement('article');
      card.className = 'agents-card';
      card.dataset.role = role;
      const title = document.createElement('h2');
      const titleText = document.createElement('span');
      titleText.textContent = ROLE_LABELS[role];
      const tag = document.createElement('span');
      tag.className = 'agents-role-tag';
      tag.textContent = role;
      title.append(titleText, tag);

      const backendLabel = document.createElement('label');
      backendLabel.textContent = '运行方式';
      const backend = document.createElement('select');
      backend.name = 'backend';
      backend.dataset.role = role;
      if (role === 'TESTER') {
        backend.append(option('sandbox', 'Docker 沙箱'));
        backend.dataset.fixed = 'true';
        backend.disabled = true;
      } else {
        backend.append(option('codex', 'Codex CLI'), option('llm', 'LLM API'));
      }
      backend.value = spec.backend;
      const backendHint = document.createElement('span');
      backendHint.className = 'field-note';
      backendHint.textContent = ROLE_HINTS[role];
      backendLabel.append(backend, backendHint);

      const modelLabel = document.createElement('label');
      modelLabel.textContent = '模型';
      const model = document.createElement('input');
      model.name = 'model';
      model.dataset.role = role;
      model.maxLength = 128;
      model.autocomplete = 'off';
      if (role === 'TESTER') {
        model.value = '';
        model.placeholder = '不调用模型';
        model.dataset.fixed = 'true';
        model.disabled = true;
      } else {
        model.value = spec.model && spec.model !== 'default' ? spec.model : '';
        model.placeholder = spec.backend === 'codex' ? '留空使用订阅默认模型' : '填写模型名称';
      }
      modelLabel.append(model);

      const dutyLabel = document.createElement('label');
      dutyLabel.textContent = '职责';
      const duty = document.createElement('textarea');
      duty.name = 'responsibility';
      duty.dataset.role = role;
      duty.maxLength = 1000;
      duty.rows = 2;
      duty.required = true;
      duty.value = spec.responsibility;
      dutyLabel.append(duty);

      card.append(title, backendLabel, modelLabel, dutyLabel);
      if (role !== 'TESTER') {
        backend.addEventListener('change', () => {
          if (state.phase !== 'ready') return;
          model.placeholder = backend.value === 'codex' ? '留空使用订阅默认模型' : '填写模型名称';
          markDirty();
        });
      }
      for (const control of [model, duty]) control.addEventListener('input', markDirty);
      fragment.append(card);
    }
    dom.cards.replaceChildren(fragment);
  }

  function markDirty() {
    if (state.phase !== 'ready') return;
    state.dirty = true;
  }

  function collectDraft() {
    const agents = {};
    for (const role of ROLE_ORDER) {
      const card = dom.cards.querySelector('[data-role="' + role + '"].agents-card');
      if (!card) throw new Error('missing role');
      const previous = state.snapshot.agents[role];
      if (role === 'TESTER') {
        const duty = card.querySelector('textarea[name="responsibility"]').value.trim();
        agents[role] = { backend: 'sandbox', model: null, responsibility: duty, tools: previous.tools, credential_ref: previous.credential_ref };
        continue;
      }
      const backend = card.querySelector('select[name="backend"]').value === 'llm' ? 'llm' : 'codex';
      const rawModel = card.querySelector('input[name="model"]').value.trim();
      const duty = card.querySelector('textarea[name="responsibility"]').value.trim();
      const model = rawModel || (backend === 'codex' ? 'default' : '');
      agents[role] = { backend, model, responsibility: duty, tools: previous.tools, credential_ref: previous.credential_ref };
    }
    return agents;
  }

  function save() {
    if (state.phase !== 'ready' || !snapshotMatches(state.projectId)) return;
    let agents;
    try {
      agents = collectDraft();
    } catch (_) {
      setError(MESSAGES.validation);
      return;
    }
    const projectId = state.projectId;
    const revision = state.revision;
    const generation = state.generation;
    cancelSave();
    const controller = new AbortController();
    state.saveAbort = controller;
    state.phase = 'saving';
    setStatus(MESSAGES.saving);
    setError('');
    setControls();
    fetch('/v1/projects/' + encodeURIComponent(projectId) + '/agents/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ revision, agents }),
      signal: controller.signal,
    }).then(async response => {
      if (generation !== state.generation || state.projectId !== projectId) return null;
      if (response.ok) {
        let saved = null;
        try {
          saved = await response.json();
        } catch (_) {
          saved = null;
        }
        if (generation !== state.generation || state.projectId !== projectId) return null;
        if (!saved || typeof saved !== 'object') {
          state.phase = 'error';
          state.dirty = true;
          setStatus('');
          setError(MESSAGES.unknownResult);
          setControls();
          return;
        }
        applySnapshot(projectId, saved);
        setStatus(MESSAGES.saved);
        return;
      }
      if (response.status === 409) {
        state.phase = 'conflict';
        state.dirty = true;
        setStatus('');
        setError(MESSAGES.conflict);
        setControls();
        return;
      }
      if (response.status === 403) {
        state.phase = 'ready';
        setStatus('');
        setError(MESSAGES.forbidden);
        setControls();
        return;
      }
      if (response.status === 404) {
        state.phase = 'ready';
        setStatus('');
        setError(MESSAGES.notFound);
        setControls();
        return;
      }
      if (response.status === 422) {
        state.phase = 'ready';
        setStatus('');
        setError(MESSAGES.validation);
        setControls();
        return;
      }
      state.phase = 'ready';
      setStatus('');
      setError(MESSAGES.server);
      setControls();
    }).catch(error => {
      if (generation !== state.generation || state.projectId !== projectId) return;
      if (error && error.name === 'AbortError') return;
      state.phase = 'ready';
      state.dirty = true;
      setStatus('');
      setError(MESSAGES.network);
      setControls();
    }).finally(() => {
      if (state.saveAbort === controller) state.saveAbort = null;
    });
  }

  function requestReload() {
    if (!state.projectId) { loadProjects(); return; }
    if (state.dirty || state.phase === 'conflict') {
      const confirmed = window.confirm(MESSAGES.reloadDiscard);
      if (!confirmed) return;
    }
    loadConfig(state.projectId);
  }

  function bind() {
    dom.project = $('agents-project');
    dom.refresh = $('agents-refresh');
    dom.status = $('agents-status');
    dom.error = $('agents-error');
    dom.cards = $('agents-cards');
    dom.release = $('agents-release');
    dom.save = $('agents-save');
    dom.form = $('agents-form');
    dom.projectOptions = [];
    dom.project.addEventListener('change', () => {
      const id = dom.project.value;
      if (!id || id === state.projectId) return;
      if (state.dirty || state.phase === 'conflict') {
        if (!window.confirm(MESSAGES.reloadDiscard)) {
          dom.project.value = state.projectId || '';
          return;
        }
      }
      loadConfig(id);
    });
    dom.refresh.addEventListener('click', requestReload);
    dom.form.addEventListener('submit', event => { event.preventDefault(); save(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { bind(); loadProjects(); });
  } else {
    bind();
    loadProjects();
  }
})();
