'use strict';
// Standalone Agent management page logic. Exposes a factory for behavioral tests.
// All environment access goes through the single IIFE-bound `root` so tests can
// inject `document`, `fetch`, `confirm` and `beforeunload` on that same object.
(function (root) {
  var ROLES = [
    { key: 'ARCHITECT', label: '架构师 ARCHITECT' },
    { key: 'CODER', label: '开发者 CODER' },
    { key: 'REVIEWER', label: '审查者 REVIEWER' },
    { key: 'TESTER', label: '测试者 TESTER' }
  ];
  var FIELDS = ['backend', 'model', 'responsibility'];

  function createAgentPage(options) {
    var opts = options || {};
    // Every dependency resolves through `root` (injected in tests, window in a
    // browser) and can be overridden explicitly via the factory options.
    var doc = opts.document || root.document;
    var fetchImpl = opts.fetch || root.fetch;
    var confirmImpl = opts.confirm || root.confirm || function () { return true; };

    var state = {
      projects: [],
      projectId: null,
      document: null,
      loadedProjectId: null,
      loadedRevision: null,
      dirty: false,
      saving: false,
      conflict: false,
      projectsGeneration: 0,
      configGeneration: 0,
      saveGeneration: 0
    };

    function el(id) { return doc.getElementById(id); }
    function setStatus(text) { el('status').textContent = text || ''; }
    function setError(text) {
      var node = el('error');
      node.textContent = text || '';
      node.classList.toggle('hidden', !text);
    }
    function clearError() { setError(''); }

    function escapeLoc(loc) {
      return loc.map(function (part) { return String(part); }).join('.');
    }

    function safeValidationDetail(body) {
      // Never surface validation `input`; only locations/messages.
      var detail = body && body.detail;
      if (typeof detail === 'string') return detail;
      if (Array.isArray(detail)) {
        return detail.slice(0, 4).map(function (item) {
          var loc = Array.isArray(item && item.loc) ? escapeLoc(item.loc.slice(1)) : '';
          var msg = typeof (item && item.msg) === 'string' ? item.msg : '输入无效';
          return loc ? loc + '：' + msg : msg;
        }).join('；');
      }
      return '';
    }

    async function request(path, requestOptions) {
      var response = await fetchImpl(path, Object.assign(
        { headers: { 'Content-Type': 'application/json' } }, requestOptions || {}));
      var body = {};
      try { body = await response.json(); } catch (_) { body = {}; }
      return { ok: response.ok, status: response.status, body: body };
    }

    function hasLoadedForm() {
      return state.document !== null && state.loadedProjectId !== null
        && state.loadedProjectId === state.projectId
        && state.loadedRevision !== null;
    }

    function updateControls() {
      var canSave = hasLoadedForm() && !state.saving && !state.conflict;
      el('save').disabled = !canSave;
      el('reload').disabled = !hasLoadedForm() || state.saving;
      // While saving, project switching and reloading are locked out so a
      // save result can never be applied to another project's form.
      el('project-select').disabled = state.saving || state.projects.length === 0;
      el('projects-retry').disabled = state.saving;
      Array.prototype.forEach.call(
        el('roles').querySelectorAll('input, textarea, select'), function (input) {
          var testerBackend = input.dataset.role === 'TESTER' && input.dataset.field === 'backend';
          input.disabled = !canSave || testerBackend;
        });
    }

    function fieldValue(value) {
      if (value === null || value === undefined) return '';
      return value;
    }

    function buildRoleCard(role, config) {
      var card = doc.createElement('section');
      card.className = 'role-card';
      card.dataset.role = role.key;
      var heading = doc.createElement('h2');
      heading.textContent = role.label;
      card.append(heading);

      FIELDS.forEach(function (field) {
        var label = doc.createElement('label');
        label.textContent = field;
        var input;
        if (field === 'responsibility') {
          input = doc.createElement('textarea');
        } else {
          input = doc.createElement('input');
          input.type = 'text';
        }
        input.id = 'agent-' + role.key + '-' + field;
        input.dataset.role = role.key;
        input.dataset.field = field;
        input.name = role.key + '.' + field;
        if (role.key === 'TESTER' && field === 'backend') {
          input.value = 'sandbox';
          input.readOnly = true;
          input.dataset.fixed = 'true';
          var note = doc.createElement('span');
          note.className = 'role-note';
          note.textContent = 'TESTER 固定使用 Docker 沙箱执行。';
          label.append(input, note);
        } else {
          input.value = fieldValue(config && config[field]);
          label.append(input);
        }
        input.addEventListener('input', function () {
          state.dirty = true;
          if (state.conflict) return;
          setStatus('有未保存的修改');
          updateControls();
        });
        card.append(label);
      });
      return card;
    }

    function readField(roleKey, field) {
      var input = doc.getElementById('agent-' + roleKey + '-' + field);
      if (!input) return '';
      if (input.dataset.fixed === 'true') return 'sandbox';
      return input.value;
    }

    function collectPayload() {
      var agents = {};
      ROLES.forEach(function (role) {
        agents[role.key] = {
          backend: role.key === 'TESTER' ? 'sandbox' : readField(role.key, 'backend'),
          model: readField(role.key, 'model'),
          responsibility: readField(role.key, 'responsibility')
        };
      });
      return { revision: state.loadedRevision, agents: agents };
    }

    function clearForm() {
      state.document = null;
      state.loadedProjectId = null;
      state.loadedRevision = null;
      state.dirty = false;
      state.conflict = false;
      el('roles').replaceChildren();
      el('agent-form').classList.add('hidden');
      setStatus('');
      updateControls();
    }

    function renderProjectOptions() {
      var select = el('project-select');
      select.replaceChildren();
      var placeholder = doc.createElement('option');
      placeholder.value = '';
      placeholder.textContent = '请选择项目';
      placeholder.disabled = true;
      select.append(placeholder);
      state.projects.forEach(function (project) {
        var option = doc.createElement('option');
        option.value = project.id;
        option.textContent = project.name + ' · ' + String(project.id).slice(0, 8);
        select.append(option);
      });
      select.value = state.projectId || '';
      select.disabled = state.saving || state.projects.length === 0;
    }

    async function loadProjects() {
      var generation = ++state.projectsGeneration;
      clearError();
      setStatus('正在加载项目…');
      el('projects-retry').classList.add('hidden');
      el('save').disabled = true;
      el('reload').disabled = true;
      try {
        var result = await request('/v1/projects');
        if (generation !== state.projectsGeneration) return;
        if (!result.ok || !result.body || !Array.isArray(result.body.projects)) {
          setStatus('');
          setError('加载项目列表失败，请重试。');
          el('projects-retry').classList.remove('hidden');
          el('projects-retry').disabled = false;
          return;
        }
        var previous = state.projectId;
        state.projects = result.body.projects.filter(function (project) {
          return project && typeof project.id === 'string' && typeof project.name === 'string';
        });
        // A reloaded list invalidates any previously loaded revision so a stale
        // form can never be saved against the new list.
        if (previous) {
          state.projectId = state.projects.some(function (p) { return p.id === previous; })
            ? previous : null;
          clearForm();
        }
        el('empty').classList.toggle('hidden', state.projects.length !== 0);
        renderProjectOptions();
        if (state.projects.length === 0) {
          setStatus('');
        } else if (!state.projectId) {
          setStatus('项目已加载，请选择项目。');
        }
        updateControls();
      } catch (_) {
        if (generation !== state.projectsGeneration) return;
        setStatus('');
        setError('加载项目列表失败，请检查网络后重试。');
        el('projects-retry').classList.remove('hidden');
      }
    }

    async function loadConfig(projectId) {
      var generation = ++state.configGeneration;
      clearError();
      clearForm();
      setStatus('正在加载配置…');
      try {
        var result = await request(
          '/v1/projects/' + encodeURIComponent(projectId) + '/agents/config');
        // Discard responses that are no longer for the current request/project.
        if (generation !== state.configGeneration || state.projectId !== projectId) return;
        if (!result.ok) {
          setStatus('');
          setError(result.status === 404 ? '项目不存在或已被删除。' : '加载配置失败，请重试。');
          return;
        }
        renderAgents(result.body || {});
        setStatus('配置已加载，revision ' + state.loadedRevision + '。');
      } catch (_) {
        if (generation !== state.configGeneration || state.projectId !== projectId) return;
        setStatus('');
        setError('加载配置失败，请检查网络后重试。');
      }
    }

    function renderAgents(documentBody) {
      state.document = documentBody || {};
      state.loadedProjectId = state.projectId;
      state.loadedRevision = typeof documentBody.revision === 'number' ? documentBody.revision : null;
      var agents = (documentBody && documentBody.agents) || {};
      var container = el('roles');
      container.replaceChildren();
      ROLES.forEach(function (role) {
        var roleConfig = agents[role.key] || {};
        var stored = {};
        FIELDS.forEach(function (field) {
          stored[field] = fieldValue(roleConfig[field]);
        });
        if (role.key === 'TESTER') stored.backend = 'sandbox';
        container.append(buildRoleCard(role, stored));
      });
      state.dirty = false;
      state.conflict = false;
      el('agent-form').classList.remove('hidden');
      updateControls();
    }

    async function save() {
      if (state.saving || state.conflict || !hasLoadedForm()) return false;
      var projectId = state.projectId;
      var payload = collectPayload();
      var saveGeneration = ++state.saveGeneration;
      state.saving = true;
      clearError();
      setStatus('正在保存…');
      updateControls();
      try {
        var result = await request(
          '/v1/projects/' + encodeURIComponent(projectId) + '/agents/config',
          { method: 'PUT', body: JSON.stringify(payload) });
        if (state.projectId !== projectId || state.saveGeneration !== saveGeneration) return false;
        if (result.ok) {
          renderAgents(result.body || {});
          setStatus('配置已保存，revision ' + state.loadedRevision + '。');
          return true;
        } else if (result.status === 409) {
          state.conflict = true;
          state.dirty = true;
          setStatus('');
          setError('配置已被其他会话修改（revision 冲突）。已保留你的草稿，' +
            '请点击“重新加载”获取最新配置后重新编辑。');
        } else if (result.status === 422) {
          setStatus('');
          setError('配置校验失败：' + (safeValidationDetail(result.body) || '请检查输入。'));
        } else if (result.status === 404) {
          setStatus('');
          setError('项目不存在或已被删除。');
        } else if (result.status === 413) {
          setStatus('');
          setError('保存内容过大，请精简后重试。');
        } else if (result.status >= 500) {
          setStatus('');
          setError('服务暂时不可用，请稍后重试。');
        } else {
          setStatus('');
          setError('保存失败，请稍后重试。');
        }
        return false;
      } catch (_) {
        if (state.projectId !== projectId || state.saveGeneration !== saveGeneration) return false;
        setStatus('');
        setError('保存失败，请检查网络后重试。');
        return false;
      } finally {
        if (state.saveGeneration === saveGeneration) {
          state.saving = false;
          updateControls();
        }
      }
    }

    function isDirtyGuard() {
      if (!state.dirty) return true;
      return confirmImpl('当前项目有未保存的修改，确定要丢弃吗？');
    }

    function onProjectChange(event) {
      var next = event.target.value;
      if (next === state.projectId) return Promise.resolve();
      if (state.saving) {
        // Locked during save; restore the currently selected project.
        event.target.value = state.projectId || '';
        return Promise.resolve();
      }
      if (!isDirtyGuard()) {
        event.target.value = state.projectId || '';
        return Promise.resolve();
      }
      state.projectId = next || null;
      clearForm();
      if (state.projectId) return loadConfig(state.projectId);
      setStatus('');
      return Promise.resolve();
    }

    function onReload() {
      if (!state.projectId || state.saving) return Promise.resolve();
      if (!isDirtyGuard()) return Promise.resolve();
      return loadConfig(state.projectId);
    }

    function onBeforeUnload(event) {
      // Guard sidebar navigation away from an edited draft. Browsers require
      // returnValue plus a returned string for widest compatibility; the string
      // itself is not shown by modern browsers but the prompt still triggers.
      if (!state || !state.dirty) return undefined;
      var message = '当前项目有未保存的修改，确定要离开吗？';
      if (event && typeof event === 'object') {
        event.returnValue = message;
      }
      return message;
    }

    function onInternalLinkClick(event) {
      // Sidebar links use real navigation triggered by beforeunload, but guard
      // any programmatic link click as well when a draft is pending.
      if (!state.dirty) return;
      var message = '当前项目有未保存的修改，确定要离开吗？';
      var accepted = typeof event.preventDefault === 'function'
        ? confirmImpl(message) : true;
      if (!accepted) {
        if (event && typeof event.preventDefault === 'function') event.preventDefault();
        return;
      }
      event.returnValue = message;
    }

    function bind() {
      el('project-select').addEventListener('change', onProjectChange);
      el('reload').addEventListener('click', onReload);
      el('projects-retry').addEventListener('click', loadProjects);
      el('save').addEventListener('click', save);
      el('agent-form').addEventListener('submit', function (event) {
        event.preventDefault();
        save();
      });
      var nav = doc.querySelector ? doc.querySelector('nav') : null;
      if (nav && nav.addEventListener) {
        nav.addEventListener('click', onInternalLinkClick);
      }
      if (root.addEventListener) {
        root.addEventListener('beforeunload', onBeforeUnload);
      }
    }

    function init() {
      bind();
      return loadProjects();
    }

    return {
      init: init,
      state: state,
      save: save,
      loadProjects: loadProjects,
      loadConfig: loadConfig,
      onProjectChange: onProjectChange,
      onReload: onReload,
      onBeforeUnload: onBeforeUnload,
      onInternalLinkClick: onInternalLinkClick
    };
  }

  root.createAgentPage = createAgentPage;

  // Auto-start only in a real browser document that is not a test harness.
  // Tests set root.__AGENTS_NO_AUTOSTART__ = true to install mocks first.
  if (root.document && root.document.getElementById && !root.__AGENTS_NO_AUTOSTART__) {
    var start = function () {
      root.agentPage = createAgentPage({});
      root.agentPage.init();
    };
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', start);
    } else {
      start();
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);
