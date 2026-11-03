'use strict';
const $ = id => document.getElementById(id);
const state = {projects: [], runs: [], project: null, projectRuns: [], view: 'overview', navigation: 0, current: null, chat: [], artifacts: [], events: [], stream: null, template: 'lru', projectId: null, file: null, revision: 0};
const labels = {QUEUED:'等待领取',RUNNING:'运行中',COMPLETED:'已完成',FAILED:'失败',INTERRUPTED:'已中断',PENDING:'等待中',BLOCKED:'已阻塞'};
const roles = [['architecture','架构师','ARCHITECT'],['coding','开发者','CODER'],['review','审查者','REVIEWER'],['testing','测试者','TESTER']];
const lruRequirement = '实现一个 Python LRU Cache，支持 get、put、len 和按最近使用顺序返回 keys。\n正确处理容量校验、更新、淘汰、空值和实例隔离。\n为实现编写单元测试，并通过平台内置的独立验收测试。';
const drafts = new Map();
let submittingWorkspace = false;
let submittingChat = false, chatButton = null, continueButton = null;
const projectManagementKey='rd-platform.project-management.v1';
let projectEdits={};
let editingProject=null, deletingProject=null;
let lastWorkspaceId=null;
try { lastWorkspaceId=localStorage.getItem('rd-platform.last-workspace'); } catch(_) {}
try { const saved=JSON.parse(localStorage.getItem(projectManagementKey) || '{}'); if(saved && typeof saved==='object' && !Array.isArray(saved)) projectEdits=saved; } catch(_) { /* Storage errors are shown if saving is attempted. */ }
function projectDisplay(project) {
  const edit=projectEdits[project.id] || {};
  return {...project,...Object.fromEntries(['name','objective','milestone'].filter(key=>typeof edit[key]==='string').map(key=>[key,edit[key]])),localState:['archived','deleted'].includes(edit.localState)?edit.localState:'active'};
}
function saveProjectEdit(id, patch) {
  const next={...projectEdits,[id]:{...projectEdits[id],...patch}};
  try { localStorage.setItem(projectManagementKey,JSON.stringify(next)); }
  catch(_) { throw new Error('浏览器无法保存修改，请检查存储权限或可用空间。'); }
  projectEdits=next; renderProjects();
}
function reportProjectOperation(message) { $('project-operation-result').textContent=message; }
function restoreWorkspaceNavigation() {
  const select=$('development-project'); select.replaceChildren();
  const projects=state.projects.map(projectDisplay).filter(item=>item.localState==='active');
  if(state.view==='project-detail' && state.project && !projects.some(item=>item.id===state.project.id)) projects.push(projectDisplay(state.project));
  const placeholder=node('option','','请选择项目'); placeholder.value=''; placeholder.disabled=true; select.append(placeholder);
  for(const project of projects) { const option=node('option','',`${project.name} · ${project.id.slice(0,8)}`); option.value=project.id; select.append(option); }
  select.value=state.view==='project-detail' ? state.project?.id || '' : '';
  select.disabled=!projects.length;
}
async function openDevelopment(push=true) {
  const projects=state.projects.map(projectDisplay).filter(item=>item.localState==='active');
  const selected=projects.find(item=>item.id===lastWorkspaceId) || projects[0];
  if(selected) return openProject(selected.id,push);
  switchView('project-detail','#development',push);
  state.project=null; state.projectRuns=[]; state.chat=[];
  $('project-detail').classList.add('development-unselected');
  $('development-empty').classList.remove('hidden');
  $('breadcrumb-current').textContent='项目开发';
  restoreWorkspaceNavigation();
}
$('workspace-execution').append($('detail'));
$('overview').insertBefore(document.querySelector('.dashboard-search'),document.querySelector('.delivery-stats'));
const terminal = status => ['COMPLETED','FAILED','INTERRUPTED'].includes(status);
const backendLabel = backend => backend === 'codex' ? 'Codex · ChatGPT 订阅' : backend === 'mixed' ? 'Codex + LLM API' : backend === 'configured' ? '项目 Agent 配置' : 'LLM API';
const stamp = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
function node(tag, className, text) { const result = document.createElement(tag); if(className) result.className = className; if(text !== undefined) result.textContent = text; return result; }
function notice(text) { $('notice').textContent = text; $('notice').classList.toggle('hidden', !text); }
async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  if(!response.ok) {
    let body = {}; try { body = await response.json(); } catch (_) {}
    throw new Error(typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.slice(0,4).map(item=>`${(item.loc || []).slice(1).join('.')}：${item.msg}`).join('；') : `请求失败 (${response.status})，请检查输入或服务状态。`);
  }
  return response.json();
}
function badge(status) { return node('span', `badge ${status}`, labels[status] || status); }
async function refreshOverview() {
  const [projects,runs]=await Promise.all([api('/v1/projects'),api('/v1/runs')]);
  state.runs=runs.runs;
  const data=deliveryView(projects.projects,runs.runs);
  state.projects = data.projects;
  const stats=data.stats;
  $('project-count').textContent = state.projects.length;
  $('stat-projects').textContent = stats.active_projects;
  $('stat-running').textContent = `共 ${stats.projects_total} 个项目 · ${stats.packages_total} 个需求包`;
  $('stat-passed').textContent = stats.delivery_percent===null ? '—' : `${stats.delivery_percent}%`;
  $('packages-summary').textContent = `${stats.packages_delivered} / ${stats.packages_total} 需求包已交付`;
  $('delivery-cycle').textContent = stats.average_delivery_seconds===null ? '—' : `${stats.average_delivery_seconds}s`;
  $('stat-failed').textContent = state.projects.filter(project=>project.status==='REVIEW_PENDING').length;
  $('delivery-footer').textContent = `活跃项目 ${stats.active_projects}　 /　 已交付需求包 ${stats.packages_delivered}　 /　 平均交付周期 ${stats.average_delivery_seconds===null ? '暂无交付记录' : stats.average_delivery_seconds+'s'}`;
  $('list-count').textContent = state.projects.length;
  $('empty').classList.toggle('hidden', state.projects.length > 0);
  renderProjects();
  restoreWorkspaceNavigation();
}
// Frontend adapter only. Delivery metadata remains optional until backend alignment.
function deliveryView(projects,runs) {
  const cycles=[];
  const items=projects.map(project=>{
    const history=runs.filter(run=>run.project_id===project.id), latest=history[0];
    const delivery=project.context?.delivery || {};
    const packages=Array.isArray(delivery.packages) ? delivery.packages.filter(p=>p && typeof p==='object') : [];
    const completed=packages.filter(p=>p.status==='COMPLETED').length;
    const duration=run=>typeof run?.summary?.elapsed_seconds==='number' && Number.isFinite(run.summary.elapsed_seconds) ? Math.max(0,run.summary.elapsed_seconds) : null;
    for(const p of packages.filter(p=>p.status==='COMPLETED')) { const seconds=duration(history.find(run=>run.id===p.run_id)); if(seconds!==null) cycles.push(seconds); }
    const status=history.some(run=>['RUNNING','QUEUED'].includes(run.status)) ? 'IN_PROGRESS' : packages.length && completed===packages.length ? 'COMPLETED' : latest?.status==='COMPLETED' ? 'REVIEW_PENDING' : ['FAILED','INTERRUPTED'].includes(latest?.status) ? 'NEEDS_ATTENTION' : 'PLANNED';
    const durations=history.map(duration).filter(value=>value!==null);
    return {id:project.id,name:project.name,objective:typeof project.context?.requirements==='string'?project.context.requirements:'',milestone:typeof delivery.milestone==='string'?delivery.milestone:null,packages_total:packages.length,packages_completed:completed,progress:packages.length?Math.round(1000*completed/packages.length)/10:null,status,elapsed_seconds:durations.length?Math.round(durations.reduce((a,b)=>a+b,0)*10)/10:null,backend:latest ? latest.backend==='codex'?'codex-cli':latest.backend==='mixed'?'codex + llm':'llm-api':project.context?.preferred_backend==='cli'?'codex-cli':project.context?.preferred_backend==='mixed'?'codex + llm':project.context?.preferred_backend==='llm'?'llm-api':null};
  });
  const total=items.reduce((n,p)=>n+p.packages_total,0),delivered=items.reduce((n,p)=>n+p.packages_completed,0);
  return {projects:items,stats:{projects_total:items.length,active_projects:items.filter(p=>p.status==='IN_PROGRESS').length,packages_total:total,packages_delivered:delivered,delivery_percent:total?Math.round(1000*delivered/total)/10:null,average_delivery_seconds:cycles.length?Math.round(cycles.reduce((a,b)=>a+b,0)/cycles.length*10)/10:null}};
}
function renderProjects() {
  const query=$('project-search').value.trim().toLocaleLowerCase();
  const scoped=state.projects.map(projectDisplay).filter(project=>project.localState===$('project-filter').value);
  const projects=scoped.filter(project=>[project.id,project.name,project.objective].join(' ').toLocaleLowerCase().includes(query));
  $('search-result-count').textContent=`${projects.length} / ${scoped.length} 个项目`;
  $('search-empty').textContent=query?'没有匹配的项目，请调整搜索条件。':'此分类暂无项目。';
  $('search-empty').classList.toggle('hidden',!state.projects.length || projects.length>0);
  $('projects').replaceChildren();
  const statusLabels={IN_PROGRESS:'开发中 IN PROGRESS',COMPLETED:'已交付 COMPLETED',REVIEW_PENDING:'待审核',NEEDS_ATTENTION:'需跟进',PLANNED:'待规划'};
  for(const project of projects) {
    const row = node('tr');
    const identity = node('td','project-identity');
    const title = node('button','project-link',project.name);
    const open = () => openProject(project.id);
    title.addEventListener('click', open);
    identity.append(title, node('small','mono',`ID: ${project.id}`));
    if(project.objective) identity.append(node('p','business-objective',project.objective));
    const status = node('td'); status.append(node('span',`badge ${project.status}`,statusLabels[project.status]));
    const progress=node('td','package-progress');
    if(project.progress===null) progress.append(node('span','muted','未登记需求包'));
    else { const bar=node('progress'); bar.max=100; bar.value=project.progress; bar.setAttribute('aria-label',`${project.name} 交付完成度 ${project.progress}%`); progress.append(bar,node('strong','',`${project.progress}%`),node('small','',`${project.packages_completed}/${project.packages_total} 需求包`)); }
    const elapsed=node('td','mono'); elapsed.append(node('span','',project.elapsed_seconds===null?'—':`${project.elapsed_seconds}s`),node('small','backend-mode',project.backend || '未选择'));
    const action = node('td'); const button = node('button','table-action','进入项目 UI →');
    button.setAttribute('aria-label',`进入项目 UI：${project.name}`);
    button.addEventListener('click',open); action.append(button);
    action.className='project-actions';
    const prdButton=node('button','table-action','编辑需求'); prdButton.addEventListener('click',()=>openRequirementEntry(project.id)); action.append(prdButton);
    const operation=(text,handler,extra='')=>{const control=node('button',`table-action ${extra}`,text);control.setAttribute('aria-label',`${text}：${project.name}`);control.addEventListener('click',handler);action.append(control);};
    if(project.localState==='deleted') {
      operation('恢复',()=>changeProjectState(project,'active','项目已从本地回收站恢复。'));
    } else {
      operation('编辑',()=>{editingProject=project.id;$('project-edit-name').value=project.name;$('project-edit-objective').value=project.objective;$('project-edit-milestone').value=project.milestone || '';$('project-edit-error').textContent='';$('project-edit-dialog').showModal();});
      operation(project.localState==='archived'?'取消归档':'归档',()=>changeProjectState(project,project.localState==='archived'?'active':'archived',project.localState==='archived'?'项目已恢复到当前列表。':'项目已归档到本浏览器的归档列表。'));
      operation('删除',()=>{deletingProject=project.id;$('project-delete-description').textContent=`将“${project.name}”移入本地回收站？`;$('project-delete-error').textContent='';$('project-delete-dialog').showModal();},'danger-link');
    }
    row.append(identity,node('td','milestone-cell',project.milestone || '待规划'),progress,status,elapsed,action);
    $('projects').append(row);
  }
}
function changeProjectState(project,localState,message) {
  try { saveProjectEdit(project.id,{localState}); reportProjectOperation(message); }
  catch(error) { reportProjectOperation(error.message); }
}
$('project-filter').addEventListener('change',renderProjects);
for(const id of ['project-edit-close','project-edit-cancel']) $(id).addEventListener('click',()=>$('project-edit-dialog').close());
for(const id of ['project-delete-close','project-delete-cancel']) $(id).addEventListener('click',()=>$('project-delete-dialog').close());
$('project-edit-form').addEventListener('submit',event=>{
  event.preventDefault(); const name=$('project-edit-name').value.trim();
  if(!name) { $('project-edit-error').textContent='请输入项目名称。'; return; }
  try { saveProjectEdit(editingProject,{name,objective:$('project-edit-objective').value.trim(),milestone:$('project-edit-milestone').value.trim()}); $('project-edit-dialog').close(); reportProjectOperation('项目展示信息已保存到此浏览器。'); }
  catch(error) { $('project-edit-error').textContent=error.message; }
});
$('project-delete-form').addEventListener('submit',event=>{
  event.preventDefault();
  try { saveProjectEdit(deletingProject,{localState:'deleted'}); $('project-delete-dialog').close(); reportProjectOperation('已移入本地回收站，可切换到回收站恢复。'); }
  catch(error) { $('project-delete-error').textContent=error.message; }
});
async function workerStatus() {
  const data = await api('/v1/workbench');
  const available = new Set(data.workers.flatMap(worker => worker.backends));
  const online = available.size > 0;
  $('worker-dot').className = `dot ${online ? 'green' : ''}`;
  $('worker-label').textContent = online ? '本地 Worker 在线' : '本地 Worker 离线';
  $('worker-label').title = online ? [...available].join(', ') : '运行 ./scripts/workbench.sh 启动本地执行器';
  if(!online) notice('执行器离线 · 可创建任务排队。请在本地终端运行 ./scripts/workbench.sh，连接 Codex 订阅与 Docker。');
  else notice('');
}
function setTemplate(template) {
  state.template = template;
  document.querySelectorAll('[data-template]').forEach(button => button.classList.toggle('selected', button.dataset.template === template));
  $('requirements').readOnly = template === 'lru';
  $('requirements').value = template === 'lru' ? lruRequirement : '';
  $('requirements').placeholder = '描述需要实现的功能、输入输出以及关键边界条件…';
  $('template-note').textContent = template === 'lru' ? '示例使用固定需求契约与独立验收测试。' : '当前支持 Python 3.12 标准库任务；生成的测试通过不等于独立业务验收。';
}
function openCreate(project=null, run=null) {
  state.projectId = project?.id || null;
  $('project-name').value = project?.name || '';
  $('project-name').readOnly = !!project;
  $('form-error').textContent = '';
  $('backend').value = run?.backend || 'codex';
  $('max-retries').value = String(run?.max_retries ?? 3);
  setTemplate(run?.template || 'lru');
  if(run?.template === 'custom') $('requirements').value = run.requirements;
  $('create-dialog').classList.add('project-only');
  $('create-dialog').querySelector('h2').textContent='创建研发项目';
  $('submit-run').textContent='创建并进入工作空间 →';
  for(const id of ['requirements','backend','max-retries']) $(id).disabled=true;
  $('create-dialog').showModal();
}
$('create-form').addEventListener('submit', async event => {
  event.preventDefault(); $('submit-run').disabled = true; $('form-error').textContent = '';
  try {
    if(!state.projectId) {
      const project = await api('/v1/projects', {method:'POST',body:JSON.stringify({name:$('project-name').value.trim()})});
      state.projectId = project.id;
    }
    $('create-dialog').close(); await refreshOverview(); await openProject(state.projectId);
  } catch(error) { $('form-error').textContent = error.message; }
  finally { $('submit-run').disabled = false; }
});
function switchView(view, hash, push=true) {
  state.stream?.close(); state.stream=null; state.current=null; ++state.revision;
  state.view=view; const navigation=++state.navigation;
  for(const [id,active] of [
    ['nav-projects',view==='overview'],
    ['nav-requirements',view==='requirement-studio'],
    ['nav-workspace',view==='project-detail'],
  ]) {
    $(id).classList.toggle('active',active);
    if(active) $(id).setAttribute('aria-current','page'); else $(id).removeAttribute('aria-current');
  }
  for(const id of ['overview','project-detail','detail','requirement-studio']) $(id)?.classList.toggle('hidden',id!==view);
  if(push && location.hash!==hash) history.pushState(null,'',location.pathname+hash);
  return navigation;
}
function showOverview(push=true) {
  switchView('overview','',push);
  $('breadcrumb-current').textContent = '项目管理'; refreshOverview().catch(error => notice(error.message));
}
async function openProject(id, push=true, selectedRun=null) {
  if(state.project) drafts.set(state.project.id,$('workspace-requirements').value);
  const navigation=switchView('project-detail',`#project/${id}`,push);
  $('project-detail').classList.remove('development-unselected');
  $('development-empty').classList.add('hidden');
  state.project=null; state.projectRuns=[];
  for(const key of ['lead-messages','feature-flow','feature-contracts','acceptance-checklist']) $(key)?.replaceChildren();
  $('project-title').textContent='加载项目…'; $('project-id').textContent=id;
  $('project-start').disabled=true; $('project-refresh').disabled=true;
  for(const key of ['project-tasks','project-runs','project-latest-status']) $(key).replaceChildren();
  for(const key of ['project-task-count','project-run-count','project-test-result']) $(key).textContent='—';
  $('workspace-requirements').value=drafts.get(id) || '';
  $('workspace-submit').disabled=true; $('workspace-error').textContent='';
  $('conversation').replaceChildren(node('p','muted','正在加载项目…')); $('team-agents').replaceChildren();
  for(const key of ['project-tasks-empty','project-runs-empty']) $(key).classList.add('hidden');
  $('breadcrumb-current').textContent='项目研发空间';
  try {
    const [project, history, agentConfig]=await Promise.all([api(`/v1/projects/${id}`),api(`/v1/runs?project_id=${encodeURIComponent(id)}`),api(`/v1/projects/${id}/agents/config`)]);
    if(navigation!==state.navigation) return;
    state.project=project; state.projectRuns=history.runs; state.agentConfig=agentConfig; renderProject();
    if(!drafts.has(id) && typeof project.context?.requirements==='string') $('workspace-requirements').value=project.context.requirements;
    if(project.context?.preferred_backend) $('workspace-backend').value=project.context.preferred_backend==='cli'?'codex':project.context.preferred_backend;
    if(agentConfig.revision > 0) $('workspace-backend').value='configured';
    else if(!project.context?.preferred_backend) $('workspace-backend').value='mixed';
    const runId=selectedRun || history.runs[0]?.id;
    if(runId) await loadRun(runId); else renderWorkspace();
  } catch(error) { if(navigation===state.navigation) { $('project-title').textContent='项目加载失败'; notice(error.message); } }
}
function renderProject() {
  const project=state.project, runs=state.projectRuns, latest=runs[0];
  lastWorkspaceId=project.id;
  try { localStorage.setItem('rd-platform.last-workspace',project.id); } catch(_) {}
  restoreWorkspaceNavigation();
  $('project-subnav').classList.remove('hidden');
  $('nav-project-group').setAttribute('aria-expanded','true');
  $('project-title').textContent=projectDisplay(project).name; $('project-id').textContent=`PROJECT ID / ${project.id}`;
  $('project-start').disabled=false; $('project-refresh').disabled=false;
  $('project-task-count').textContent=project.tasks.length; $('project-run-count').textContent=runs.length;
  $('project-latest-status').replaceChildren(latest ? badge(latest.status) : node('span','muted','尚未运行'));
  const tests=latest?.summary?.final_test_summary;
  $('project-test-result').textContent=tests?.counts ? `${tests.counts.passed} / ${tests.total}` : '—';
  $('project-tasks').replaceChildren();
  for(const task of project.tasks) {
    const row=node('tr'), identity=node('td','project-identity'), status=node('td'), details=node('td');
    identity.append(node('span','',task.name),node('small','mono',task.id)); status.append(badge(task.status));
    const disclosure=node('details','task-data'); disclosure.append(node('summary','','查看数据'),node('pre','',JSON.stringify({input:task.input,output:task.output,error:task.error},null,2))); details.append(disclosure);
    row.append(identity,node('td','mono',task.capability),status,node('td','task-dependencies',task.depends_on.join(', ') || '—'),details); $('project-tasks').append(row);
  }
  $('project-tasks-empty').classList.toggle('hidden',project.tasks.length>0);
  $('project-runs').replaceChildren();
  for(const run of runs) {
    const row=node('tr'), identity=node('td','project-identity'), status=node('td'), action=node('td');
    identity.append(node('span','mono',run.id.slice(0,8)),node('small','',run.template==='lru' ? 'LRU Cache · 固定验收' : run.template==='resume' ? '从 Reviewer 恢复' : '自定义 Python'));
    status.append(badge(run.status)); const tests=run.summary?.final_test_summary;
    const button=node('button','table-action','运行详情 →'); button.setAttribute('aria-label',`运行详情：${run.id.slice(0,8)}`); button.addEventListener('click',()=>openRun(run.id)); action.append(button);
    row.append(identity,status,node('td','',backendLabel(run.backend)),node('td','mono',tests?.counts ? `${tests.counts.passed} / ${tests.total}` : '—'),node('td','mono',stamp(run.created_at)),action); $('project-runs').append(row);
  }
  $('project-runs-empty').classList.toggle('hidden',runs.length>0);
}
async function openRun(id, push=true) {
  try {
    const navigation=state.navigation;
    const run=await api(`/v1/runs/${id}`);
    if(navigation!==state.navigation) return;
    await openProject(run.project_id,push,id);
  } catch(error) { notice(error.message); }
}
async function loadRun(id) {
  const navigation=state.navigation;
  state.stream?.close(); state.stream=null; state.current=null; ++state.revision;
  $('detail').classList.remove('hidden');
  state.events=[]; state.file=null; state.artifacts=[]; state.chat=[];
  $('detail-title').textContent='加载运行…'; $('rerun').disabled=true; $('resume-review').classList.add('hidden'); $('resume-node').classList.add('hidden'); $('download').disabled=true;
  $('flow').replaceChildren(); $('artifact-files').replaceChildren(); $('timeline').replaceChildren();
  for(const key of ['detail-subtitle','run-short','run-status','run-backend','run-time','run-tests','code-title','code-content','log-content','requirements-content']) $(key).textContent='';
  $('run-error').classList.add('hidden'); $('run-history')?.replaceChildren(); selectTab('artifacts');
  try {
    const run=await api(`/v1/runs/${id}`);
    if(navigation!==state.navigation) return;
    const [project, history]=await Promise.all([api(`/v1/projects/${run.project_id}`),api(`/v1/runs?project_id=${encodeURIComponent(run.project_id)}`)]);
    if(navigation!==state.navigation) return;
    state.current=run; state.project=project; state.projectRuns=history.runs; $('download').disabled=false;
    $('breadcrumb-current').textContent = '项目研发空间'; renderRun(); renderTimeline(); renderWorkspace();
    await refreshChat(id);
    await refreshArtifacts(id);
    if(navigation!==state.navigation) return;
    const stream = new EventSource(`/v1/runs/${id}/events`); state.stream = stream;
    stream.addEventListener('update', event => {
      if(state.current?.id !== id || navigation!==state.navigation) return;
      const data = JSON.parse(event.data);
      if(!state.events.some(item => item.id === data.id)) state.events.push(data);
      state.events = state.events.slice(-200); renderTimeline();
      refreshRun(id).catch(error => notice(error.message));
    });
    stream.addEventListener('done', () => { stream.close(); if(navigation!==state.navigation) return; $('event-status').textContent='事件已归档'; refreshRun(id).catch(error => notice(error.message)); });
    stream.onopen = () => { if(navigation===state.navigation) $('event-status').textContent='实时事件已连接'; };
    stream.onerror = () => { if(navigation===state.navigation) $('event-status').textContent='连接中断，自动重连…'; };
  } catch(error) { if(navigation===state.navigation) notice(error.message); }
}
async function refreshRun(id) {
  const revision = ++state.revision;
  const run = await api(`/v1/runs/${id}`);
  if(state.current?.id !== id || revision !== state.revision) return;
  state.current=run;
  state.runs=state.runs.map(item=>item.id===run.id ? {...item,status:run.status,summary:run.summary} : item);
  state.projectRuns=state.projectRuns.map(item=>item.id===run.id ? {...item,status:run.status,summary:run.summary} : item);
  renderRun(); renderWorkspace(); await refreshArtifacts(id);
}
async function refreshChat(id) {
  const [data,history]=await Promise.all([api(`/v1/runs/${id}/chat`),
    state.project ? api(`/v1/runs?project_id=${encodeURIComponent(state.project.id)}`) : Promise.resolve({runs:[]})]);
  if(state.current?.id!==id) return;
  state.chat=data.messages;
  state.projectRuns=history.runs;
  renderProductWorkspace();
}

function latestRelatedRun(run) {
  if(!run) return null;
  let frontier=new Set([run.id]), latest=null;
  for(let depth=0;depth<4;depth++) {
    const children=state.projectRuns.filter(item=>item.template==='resume' && frontier.has(item.summary?.parent_run_id));
    if(!children.length) break;
    for(const item of children) if(!latest || item.created_at>latest.created_at) latest=item;
    frontier=new Set(children.map(item=>item.id));
  }
  return latest;
}
function renderRun() {
  const run=state.current;
  const project=state.project;
  $('detail-title').textContent = project?.name || '研发任务';
  $('run-short').textContent = `RUN / ${run.id.slice(0,8).toUpperCase()}`;
  $('detail-subtitle').textContent = run.template === 'lru' ? 'LRU Cache · 从需求契约到独立沙箱验收' : '自定义 Python 任务 · 标准库执行环境';
  $('run-status').className = `badge ${run.status}`; $('run-status').textContent = labels[run.status];
  $('run-backend').textContent=backendLabel(run.backend); $('run-time').textContent=`创建于 ${stamp(run.created_at)}`;
  let historySelect=$('run-history');
  if(!historySelect) { historySelect=node('select'); historySelect.id='run-history'; historySelect.setAttribute('aria-label','历史运行'); historySelect.addEventListener('change',()=>openRun(historySelect.value)); $('run-time').parentElement.append(historySelect); }
  historySelect.replaceChildren();
  const history=state.projectRuns.some(item=>item.id===run.id) ? state.projectRuns : [run,...state.projectRuns];
  for(const item of history) { const option=node('option','',`${stamp(item.created_at)} · ${labels[item.status]}`); option.value=item.id; historySelect.append(option); }
  historySelect.value=run.id;
  $('rerun').disabled = !terminal(run.status);
  const checkpoint = ['custom','resume'].includes(run.template) && ['FAILED','INTERRUPTED'].includes(run.status) && run.tasks.length>0;
  $('resume-review').classList.toggle('hidden',!checkpoint);
  $('resume-node').classList.toggle('hidden',!checkpoint);
  $('resume-review').disabled=!checkpoint;
  if(checkpoint) {
    const select=$('resume-node'); select.replaceChildren();
    for(const task of run.tasks) {
      const label=roles.find(item=>item[0]===task.capability)?.[1] || task.capability;
      const option=node('option','',`${label} · ${task.id}`); option.value=task.id; select.append(option);
    }
    select.value=[...run.tasks].reverse().find(task=>['FAILED','BLOCKED'].includes(task.status))?.id || run.tasks.at(-1).id;
  }
  const test=run.summary.final_test_summary || [...run.tasks].reverse().find(task => task.capability==='testing')?.output?.test_summary;
  $('run-tests').textContent = test?.counts ? `${test.counts.passed}/${test.total} 测试通过` : '等待测试证据';
  $('iteration-label').textContent = run.summary.iteration_count !== undefined ? `${run.summary.iteration_count} 次修订 · ${run.summary.elapsed_seconds?.toFixed(1) || '—'} s` : `${run.tasks.length} 个角色任务 · 最多 ${run.max_retries} 次修订`;
  $('requirements-content').textContent=run.requirements;
  $('acceptance-label').textContent = run.template==='lru' ? '独立验收：固定 LRU 契约' : '验收范围：模型生成的测试';
  $('run-error').textContent=run.summary.error || ''; $('run-error').classList.toggle('hidden',!run.summary.error);
  $('flow').replaceChildren();
  roles.forEach(([capability,title,role], index) => {
    const task=[...run.tasks].reverse().find(item => item.capability===capability);
    const status=task?.status || 'PENDING'; const rejected=task?.output?.review_status==='REJECTED';
    const button=node('button',`role ${rejected ? 'BLOCKED' : status}`);
    const top=node('div','role-top'); top.append(node('span','',`0${index+1}`),node('span','',status==='COMPLETED'?'✓':status==='RUNNING'?'◌':'○'));
    const roleLabel=task?.output?.review_status ? (rejected ? '需修订 · REJECTED' : '审查通过 · APPROVED') : labels[status];
    button.append(top,node('span','role-title',title),node('span','role-sub',role),node('span','role-state',roleLabel));
    button.addEventListener('click',()=>{ const artifact=state.artifacts.find(item=>item.task_id===task?.id); if(artifact)selectFile(artifact); else selectTab('logs'); });
    $('flow').append(button);
  });
  $('log-content').textContent = run.tasks.map(task => {
    const output=task.output || {}; const evidence=output.evidence_logs || {};
    const logs=[...new Set([task.error,task.logs,evidence.logs].filter(Boolean))].join('\n');
    return `[${task.id}] ${labels[task.status] || task.status}\n${logs}${output.test_summary ? '\n'+JSON.stringify(output.test_summary,null,2) : ''}`;
  }).join('\n\n') || '任务已入队，等待本地 Worker 领取。';
  if(run.summary.evidence_dir) $('log-content').textContent += `\n\n本地证据目录：${run.summary.evidence_dir}`;
}
async function refreshArtifacts(id) {
  const response = await api(`/v1/runs/${id}/artifacts`);
  if(state.current?.id !== id) return;
  state.artifacts=response.artifacts;
  $('artifact-files').replaceChildren();
  for(const artifact of state.artifacts) {
    const button=node('button','file-button',artifact.path); button.append(node('small','',artifact.task_id));
    button.dataset.file=`${artifact.task_id}/${artifact.path}`;
    button.addEventListener('click',()=>selectFile(artifact)); $('artifact-files').append(button);
  }
  const selected=state.artifacts.find(item=>`${item.task_id}/${item.path}`===state.file) || state.artifacts[0];
  if(selected) paintFile(selected);
  else { $('code-title').textContent='等待产物'; $('code-content').textContent='团队完成角色任务后，产物将在这里展示。'; }
}
function paintFile(artifact) {
  state.file=`${artifact.task_id}/${artifact.path}`;
  $('code-title').textContent=artifact.path; $('code-content').textContent=artifact.content;
  document.querySelectorAll('.file-button').forEach(button=>button.classList.toggle('selected',button.dataset.file===state.file));
}
function selectFile(artifact) { paintFile(artifact); selectTab('artifacts'); }
function selectTab(name) {
  document.querySelectorAll('[data-tab]').forEach(button=>button.classList.toggle('active',button.dataset.tab===name));
  for(const tab of ['artifacts','logs','requirements']) $(`tab-${tab}`).classList.toggle('hidden',tab!==name);
}
function renderTimeline() {
  $('timeline').replaceChildren();
  for(const event of state.events) {
    const text=event.kind==='task_started' ? `${event.data.name} · 开始执行` : event.kind==='task_finished' ? `${event.data.name} · ${labels[event.data.status]}` : labels[event.data.status] || event.kind;
    const item=node('li','',text); item.append(node('small','',new Date(event.created_at*1000).toLocaleTimeString('zh-CN'))); $('timeline').append(item);
  }
}
function renderWorkspace() {
  const run=state.current;
  const active=state.projectRuns.some(item=>['QUEUED','RUNNING'].includes(item.status));
  $('workspace-submit').disabled=submittingWorkspace || active || !state.project;
  if(chatButton) chatButton.disabled=!run || submittingChat;
  if(continueButton) {
    continueButton.classList.toggle('hidden',!run || !['FAILED','INTERRUPTED'].includes(run.status));
    continueButton.disabled=!run || submittingChat;
  }
  $('workspace-note').textContent=active ? '团队正在执行；可向 Team Lead 追问本轮进展。启动新需求会创建独立运行。' : '发送消息用于本轮讨论；点击“继续最新运行”才会启动恢复动作。启动新需求会创建独立运行。';
  const history=$('workspace-history'); history.replaceChildren();
  if(!state.projectRuns.length) history.append(node('option','','暂无执行记录'));
  for(const item of state.projectRuns) { const option=node('option','',`${stamp(item.created_at)} · ${item.id.slice(0,8)} · ${labels[item.status]}`); option.value=item.id; history.append(option); }
  history.disabled=!state.projectRuns.length;
  if(run) history.value=run.id;
  $('conversation').replaceChildren();
  if(run) appendAgentDialogue({speaker:'Team Lead',target:'@Architect',role:'lead',status:run.status,
    text:`请根据本轮需求「${requirementTitle(run.requirements)}」完成契约拆解，并将可执行方案交给 Coder。`});
  for(const task of run?.tasks || []) {
    const role=roles.find(item=>item[0]===task.capability);
    const output=task.output || {};
    const handoff={architecture:'@Coder',coding:'@Reviewer',review:output.review_status==='REJECTED'?'@Coder':'@Tester',testing:'@TeamLead'};
    appendAgentDialogue({speaker:role?.[2] || task.name,target:handoff[task.capability] || '@TeamLead',role:task.capability,
      status:task.status,text:agentDialogueText(task),task});
  }
  if(run?.status==='COMPLETED') appendAgentDialogue({speaker:'Team Lead',target:'@团队',role:'lead',status:'COMPLETED',
    text:'收到 Tester 的验收结果。本轮研发已完成，交付物和业务验收清单已更新，请等待人工确认。'});
  if(run && !run.tasks.length) appendAgentDialogue({speaker:'Team Lead',target:'@团队',role:'lead',status:run.status,
    text:run.status==='QUEUED' ? '研发任务已进入队列，等待团队领取。' : labels[run.status]});
  if(!run) $('conversation').append(node('p','panel-empty','选择或提交研发任务后，这里会显示 Agent 之间的真实交接对话。'));
  $('team-agents').replaceChildren();
  for(const [capability,title,role] of roles) {
    const task=[...(run?.tasks || [])].reverse().find(item=>item.capability===capability);
    const button=node('button','team-agent');
    const heading=node('div','message-heading'); heading.append(node('strong','',title),badge(task?.status || 'PENDING'));
    button.append(heading,node('small','mono',role));
    button.addEventListener('click',()=>{const artifact=state.artifacts.find(item=>item.task_id===task?.id); if(artifact)selectFile(artifact); else selectTab('logs'); if(run) $('workspace-execution').scrollIntoView({behavior:'smooth',block:'start'});});
    $('team-agents').append(button);
  }
  renderProductWorkspace();
}

function requirementTitle(text) {
  try { const data=JSON.parse(text); if(typeof data.title==='string' && data.title.trim()) return data.title.trim(); } catch(_) {}
  return text.split(/\n/)[0].slice(0,80) || '未命名需求';
}

function agentDialogueText(task) {
  const output=task.output || {};
  if(task.error) return '本阶段遇到问题，已停止向下游交接。错误详情已收录到技术证据。';
  if(task.status==='RUNNING') return '我正在处理当前阶段，完成后会在这里向下一位成员交接。';
  if(task.status!=='COMPLETED') return `当前阶段状态：${labels[task.status] || task.status}。`;
  if(task.capability==='architecture') {
    const modules=(output.architecture_spec?.modules || []).map(item=>item.name);
    const contracts=Object.keys(output.api_contracts || {});
    return `需求契约已拆解完成，共形成 ${(output.subtasks || []).length} 个开发子任务、${contracts.length} 份接口契约。${modules.length ? `模块包括：${modules.join('、')}。` : ''}请按这些边界完成实现与单元测试。`;
  }
  if(task.capability==='coding') {
    const files=Object.keys(output.files || {}), tests=Object.keys(output.tests || {});
    return `实现已经提交：${files.join('、') || '业务代码'}；测试：${tests.join('、') || '待补充'}。变更摘要：${output.commit_message || '已按架构契约完成实现'}。请重点检查边界条件和异常路径。`;
  }
  if(task.capability==='review') {
    const issues=output.issues || [];
    if(output.review_status==='REJECTED') return `本轮审查未通过，发现 ${issues.length} 个问题：${issues.slice(0,3).map(item=>item.message).join('；') || '请按修正指令调整实现'}。修复后请重新提交审查。`;
    return `代码审查通过。已核对实现范围、接口契约与安全边界${issues.length ? `，另有 ${issues.length} 条非阻塞建议` : ''}。请进入隔离环境执行验收。`;
  }
  if(task.capability==='testing') {
    const summary=output.test_summary || {}, counts=summary.counts || {};
    return `隔离沙箱验收完成：${counts.passed ?? 0}/${summary.total ?? 0} 通过，结果 ${summary.outcome || '已记录'}。测试证据已归档，可以交给 Team Lead 汇总。`;
  }
  return '当前阶段已完成，交付物已传给下一位成员。';
}

function appendAgentDialogue({speaker,target,role,status,text,task=null}) {
  const item=node('article',`agent-dialogue dialogue-${role}`), avatar=node('span','agent-avatar',speaker.slice(0,1));
  const body=node('div','dialogue-body'), heading=node('div','dialogue-heading');
  heading.append(node('strong','',speaker),node('span','dialogue-target',target),badge(status));
  body.append(heading,node('p','dialogue-bubble',text));
  if(task) {
    const button=node('button','dialogue-attachment','查看本次交付物 →');
    button.addEventListener('click',()=>{evidenceDisclosure.open=true;const artifact=state.artifacts.find(item=>item.task_id===task.id);if(artifact)selectFile(artifact);else selectTab('logs');$('workspace-execution').scrollIntoView({behavior:'smooth',block:'start'});});
    body.append(button);
  }
  item.append(avatar,body);$('conversation').append(item);
}
$('workspace-history').addEventListener('change',()=>openProject(state.project.id,false,$('workspace-history').value));
$('workspace-example').addEventListener('click',()=>{ $('workspace-requirements').value=lruRequirement; $('workspace-requirements').focus(); });
$('workspace-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if(submittingWorkspace || !state.project || $('workspace-submit').disabled) return;
  const projectId=state.project.id, navigation=state.navigation, requirements=$('workspace-requirements').value.trim();
  if(!requirements) { $('workspace-error').textContent='请输入研发需求。'; return; }
  submittingWorkspace=true; $('workspace-submit').disabled=true; $('workspace-error').textContent='';
  try {
    const example=requirements===lruRequirement;
    const run=await api('/v1/runs',{method:'POST',body:JSON.stringify({project_id:projectId,requirements,
      template:example?'lru':'custom',workspace_mode:example?'isolated':'repository',
      backend:$('workspace-backend').value,max_retries:Number($('workspace-retries').value)})});
    drafts.delete(projectId);
    if(navigation===state.navigation) { $('workspace-requirements').value=''; await openProject(projectId,false,run.id); }
  } catch(error) { if(navigation===state.navigation) $('workspace-error').textContent=error.message; }
  finally { submittingWorkspace=false; if(state.view==='project-detail' && state.project) renderWorkspace(); }
});
$('project-search').addEventListener('input',renderProjects);
for(const id of ['new-project','empty-create']) $(id).addEventListener('click',()=>{ $('delivery-error').textContent=''; $('delivery-dialog').showModal(); });
$('delivery-close').addEventListener('click',()=>$('delivery-dialog').close());
$('delivery-form').addEventListener('submit',async event=>{
  event.preventDefault(); $('delivery-submit').disabled=true; $('delivery-error').textContent='';
  try {
    const requirements=$('delivery-requirements').value.trim();
    const project=await api('/v1/projects',{method:'POST',body:JSON.stringify({name:$('delivery-name').value.trim(),context:{requirements,preferred_backend:$('delivery-backend').value,delivery:{milestone:$('delivery-milestone').value.trim(),packages:[{id:'P1',name:$('delivery-name').value.trim(),status:'PENDING'}]}}})});
    $('delivery-dialog').close(); $('delivery-form').reset();
    await refreshOverview(); await openProject(project.id);
    $('workspace-requirements').value=requirements;
    $('workspace-backend').value=project.context.preferred_backend==='cli'?'codex':project.context.preferred_backend;
  } catch(error) { $('delivery-error').textContent=error.message; }
  finally { $('delivery-submit').disabled=false; }
});
for(const id of ['project-back','nav-projects']) $(id).addEventListener('click',()=>showOverview());
$('nav-project-group').addEventListener('click',()=>{
  const expanded=$('nav-project-group').getAttribute('aria-expanded')!=='true';
  $('nav-project-group').setAttribute('aria-expanded',String(expanded));
  $('project-subnav').classList.toggle('hidden',!expanded);
});
$('nav-workspace').addEventListener('click',()=>{
  openDevelopment().catch(error=>notice(error.message));
});
$('nav-requirements').addEventListener('click',async()=>{
  if(studioDirty && state.view==='requirement-studio') {
    notice('当前需求有未保存修改，请先保存草稿。');
    return;
  }
  const projects=state.projects.map(projectDisplay).filter(item=>item.localState==='active');
  const selected=projects.find(item=>item.id===lastWorkspaceId) || projects[0];
  if(!selected) {
    notice('暂无可编辑需求的项目，请先新建项目。');
    return;
  }
  await openRequirementEntry(selected.id);
});
$('development-project').addEventListener('change',()=>openProject($('development-project').value));
$('back').addEventListener('click',()=>state.current ? openProject(state.current.project_id) : showOverview());
$('project-start').addEventListener('click',()=>openRequirementEntry(state.project.id));
$('project-refresh').addEventListener('click',()=>openProject(state.project.id,false));
$('close-dialog').addEventListener('click',()=>$('create-dialog').close());
$('refresh').addEventListener('click',()=>refreshOverview().catch(error=>notice(error.message)));
$('rerun').addEventListener('click',()=>{ $('workspace-requirements').value=state.current.requirements; $('workspace-requirements').focus(); $('workspace-form').scrollIntoView({behavior:'smooth',block:'center'}); });
$('resume-review').addEventListener('click',async()=>{
  const run=state.current;
  if(!run) return;
  $('resume-review').disabled=true;
  try {
    const taskId=$('resume-node').value;
    const resumed=await api(`/v1/runs/${run.id}/resume`,{method:'POST',body:JSON.stringify({task_id:taskId})});
    await openProject(run.project_id,true,resumed.id);
    notice(`已创建恢复运行：从 ${taskId} 节点继续。`);
  } catch(error) {
    $('resume-review').disabled=false;
    notice(error.message);
  }
});
document.querySelectorAll('[data-template]').forEach(button=>button.addEventListener('click',()=>setTemplate(button.dataset.template)));
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>selectTab(button.dataset.tab)));
$('download').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify({run:state.current,artifacts:state.artifacts},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob); const link=node('a'); link.href=url; link.download=`run-${state.current.id}.json`; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
});
async function route() {
  const prd=location.hash.match(/^#requirements\/([a-f0-9]{32})\/([a-f0-9]{32})$/);
  if(prd) return openStudio(prd[1],prd[2],false);
  if(location.hash==='#development') return openDevelopment(false);
  const project=location.hash.match(/^#project\/([a-f0-9]{32})$/), run=location.hash.match(/^#run\/([a-f0-9]{32})$/);
  if(project) await openProject(project[1],false); else if(run) await openRun(run[1],false); else showOverview(false);
}
window.addEventListener('popstate',route);
async function init() { try { await Promise.all([refreshOverview(),workerStatus()]); await route(); } catch(error){notice(error.message);} }
init();
setInterval(()=>{workerStatus().catch(()=>notice('服务连接中断，请检查 ./scripts/deploy.sh status')); if(state.view==='overview')refreshOverview().catch(()=>{});},10000);

// Three-tier workspace: business-facing collaboration with opt-in execution evidence.
const squadPanel=document.querySelector('.collaboration-panel');
const preview=node('section','product-preview');
preview.append(node('div','eyebrow','PRODUCT DELIVERY'),node('h2','','产品功能与交付物预览'));
for(const id of ['feature-flow','feature-contracts','acceptance-checklist']) { const section=node('div'); section.id=id; preview.append(section); }
document.querySelector('.workspace-grid').append(preview);
document.querySelector('.team-panel').classList.add('hidden');
const leadPanel=node('section','lead-panel');
leadPanel.append(node('div','eyebrow','HUMAN ↔ TEAM LEAD'),node('h2','','与团队负责人对话'));
const leadMessages=node('div'); leadMessages.id='lead-messages'; leadMessages.setAttribute('aria-live','polite');
leadPanel.append(leadMessages,$('workspace-form'));
document.querySelector('.workspace-grid').after(leadPanel);
squadPanel.querySelector('h2').textContent='多 Agent 团队对话';
chatButton=node('button','secondary','发送给 Team Lead'); chatButton.type='button'; chatButton.disabled=true;
$('workspace-submit').before(chatButton);
continueButton=node('button','secondary hidden','继续最新运行'); continueButton.type='button'; continueButton.disabled=true;
chatButton.after(continueButton);
$('workspace-submit').textContent='启动新需求 →';
$('workspace-form').querySelector('label[for="workspace-requirements"]').textContent='新需求、当前运行的追问或续跑指令';
$('workspace-requirements').placeholder='询问本轮进展、失败原因或提出修改建议…';
chatButton.addEventListener('click',async()=>{
  const runId=state.current?.id, message=$('workspace-requirements').value.trim();
  if(!runId) { $('workspace-error').textContent='请先选择一条运行记录。'; return; }
  if(!message) { $('workspace-error').textContent='请输入要讨论的问题。'; return; }
  if(submittingChat) return;
  submittingChat=true; chatButton.disabled=true; $('workspace-error').textContent='';
  try {
    await api(`/v1/runs/${runId}/chat`,{method:'POST',body:JSON.stringify({message})});
    if(state.current?.id===runId) { $('workspace-requirements').value=''; await refreshChat(runId); }
  } catch(error) { $('workspace-error').textContent=error.message; }
  finally { submittingChat=false; chatButton.disabled=!state.current; }
});
setInterval(()=>{ if(state.view==='project-detail' && state.current) refreshChat(state.current.id).catch(()=>{}); },8000);
continueButton.addEventListener('click',async()=>{
  const runId=state.current?.id;
  if(!runId || submittingChat) return;
  submittingChat=true; continueButton.disabled=true; $('workspace-error').textContent='';
  try {
    await api(`/v1/runs/${runId}/chat`,{method:'POST',body:JSON.stringify({message:'继续当前研发运行',action:'resume'})});
    if(state.current?.id===runId) await refreshChat(runId);
  } catch(error) { $('workspace-error').textContent=error.message; }
  finally { submittingChat=false; if(state.current) renderWorkspace(); }
});
const evidenceDisclosure=node('details','technical-evidence'); evidenceDisclosure.append(node('summary','','技术证据与源码（按需展开）'));
$('workspace-execution').before(evidenceDisclosure); evidenceDisclosure.append($('workspace-execution'));
function workspaceData() { return state.project?.context?.workspace || {revision:0,milestones:[],accepted:[]}; }
async function persistWorkspace(next) {
  const id=state.project.id, navigation=state.navigation;
  const project=await api(`/v1/projects/${id}/workspace`,{method:'PUT',body:JSON.stringify(next)});
  if(state.project?.id===id && state.navigation===navigation) { state.project=project; renderProductWorkspace(); }
}
function renderProductWorkspace() {
  if(!state.project) return;
  const run=state.current, data=workspaceData(), tasks=run?.tasks || [];
  const related=latestRelatedRun(run), latest=related || run;
  const architecture=[...tasks].reverse().find(t=>t.capability==='architecture' && t.output?.architecture_spec)?.output;
  const latestTask=tasks.at(-1), role=roles.find(r=>r[0]===latestTask?.capability)?.[1];
  const failedReview=run?.status==='FAILED' && latestTask?.capability==='review' && latestTask?.error?.includes('ValidationError');
  const passed=latest?.summary?.final_test_summary;
  const lead=related?.status==='COMPLETED' ? `关联恢复运行 ${related.id.slice(0,8)} 已完成，Tester ${passed?.counts?.passed ?? 0}/${passed?.total ?? 0} 项测试通过。验收范围为生成代码附带的测试，产物尚未部署到项目。` :
    related?.status==='RUNNING'||related?.status==='QUEUED' ? `关联恢复运行 ${related.id.slice(0,8)} 正在${related.status==='RUNNING'?'执行':'等待领取'}；原运行的失败状态作为历史记录保留。` :
    related?.status==='FAILED'||related?.status==='INTERRUPTED' ? `关联恢复运行 ${related.id.slice(0,8)} 仍需处理。可进入该运行查看最新失败证据并选择节点继续。` :
    run?.status==='COMPLETED'?'团队已完成本轮实现与自动化验证。请检查右侧交付物，逐项确认业务验收标准。':run?.status==='QUEUED'?'需求已接收，正在等待团队领取。':run?.status==='RUNNING'?`团队正在推进${role || '研发'}阶段。审查或验证发现的问题将在本轮内交回开发者修订。`:failedReview?'Reviewer 的结构化输出未通过格式校验，本轮已停止。可在运行详情中选择恢复节点。':run?'本轮研发需要跟进。可向 Team Lead 询问原因，或选择节点继续。':'请描述业务目标、功能范围和验收标准，我将通过现有调度流程交给研发团队。';
  $('lead-messages').replaceChildren();
  if(run) { const request=node('article','message user-message'); request.append(node('strong','','你 · 本轮需求'),node('p','message-content',businessRequirementText(run.requirements))); $('lead-messages').append(request); }
  const report=node('article','message lead-message'); report.append(node('strong','','Team Lead · 状态汇总'),node('p','',lead)); $('lead-messages').append(report);
  if(related) { const link=node('button','table-action','查看最新恢复运行 →'); link.addEventListener('click',()=>openProject(state.project.id,true,related.id)); report.append(link); }
  for(const item of state.chat) {
    const question=node('article','message user-message');
    question.append(node('strong','','你 · 本轮讨论'),node('p','message-content',item.message));
    $('lead-messages').append(question);
    const reply=node('article','message lead-message');
    const answer=item.answer || (item.status==='FAILED' ? item.error || '回复失败，请重试。' : 'Team Lead 正在整理本轮上下文，等待回复…');
    reply.append(node('strong','',`Team Lead · ${item.resume_run_id?'已启动恢复':item.status==='COMPLETED'?'回复':item.status==='FAILED'?'回复失败':'处理中'}`),node('p','message-content',answer));
    if(item.resume_run_id) {
      const link=node('button','table-action','进入恢复运行 →');
      link.addEventListener('click',()=>openProject(state.project.id,true,item.resume_run_id));
      reply.append(link);
    }
    $('lead-messages').append(reply);
  }
  const userCard=$('conversation').querySelector('.user-message'); userCard?.remove();
  if(!tasks.length) $('conversation').replaceChildren(node('p','panel-empty','团队领取任务后，架构、开发、审查与验收的阶段交接将在这里显示。'));
  $('feature-flow').replaceChildren(node('h3','','业务流程'));
  const flow=architecture?.architecture_spec?.data_flow || [];
  flow.forEach((step,index)=>$('feature-flow').append(node('div','business-step',`${index+1}. ${step}`)));
  if(!flow.length) $('feature-flow').append(node('p','muted','等待架构师提交业务流程。'));
  $('feature-contracts').replaceChildren(node('h3','','接口与功能说明'));
  for(const module of architecture?.architecture_spec?.modules || []) $('feature-contracts').append(node('p','',`${module.name}：${module.responsibility}`));
  for(const [name,contract] of Object.entries(architecture?.api_contracts || {})) { const detail=node('details'); detail.append(node('summary','',name),node('pre','',JSON.stringify(contract,null,2))); $('feature-contracts').append(detail); }
  $('acceptance-checklist').replaceChildren(node('h3','','业务验收清单'),node('p','field-note','人工确认 · 按运行版本保存；勾选不会改变自动化测试结果。'));
  const activePRD=state.project.context?.active_requirement;
  const requirement=activePRD && (!run || !activePRD.run_id || activePRD.run_id===run.id) ? activePRD : null;
  const criteria=requirement ? requirement.document.acceptance_criteria : [...new Set((architecture?.subtasks || []).flatMap(task=>task.acceptance_criteria || []))];
  if(!criteria.length) $('acceptance-checklist').append(node('p','muted','尚无验收条目，等待架构产物。'));
  for(const criterion of criteria) {
    const key=JSON.stringify([run?.id,criterion]), label=node('label','acceptance-item'), check=node('input'); check.type='checkbox'; check.checked=requirement ? requirement.accepted.includes(criterion) : data.accepted.includes(key);
    if(requirement) {
      check.addEventListener('change',async()=>{
        check.disabled=true; const projectId=state.project.id,navigation=state.navigation;
        try {
          const path=`/v1/projects/${projectId}/requirements/${requirement.id}`, draft=await api(path);
          if(draft.revision!==requirement.revision) throw new Error('验收清单已更新，请刷新后重试。');
          const accepted=new Set(draft.accepted); if(check.checked)accepted.add(criterion);else accepted.delete(criterion);
          await api(path,{method:'PUT',body:JSON.stringify({revision:draft.revision,document:draft.document,accepted:[...accepted],messages:draft.messages,applied_analysis_id:draft.applied_analysis_id || null,ai_review:draft.ai_review || null})});
          const project=await api(`/v1/projects/${projectId}`);if(navigation===state.navigation){state.project=project;renderProductWorkspace();}
        } catch(error){check.checked=!check.checked;notice(error.message);}finally{check.disabled=false;}
      });
      label.append(check,node('span','',criterion));$('acceptance-checklist').append(label);continue;
    }
    check.addEventListener('change',async()=>{ check.disabled=true; try { const current=workspaceData(), accepted=new Set(current.accepted); if(check.checked) accepted.add(key); else accepted.delete(key); await persistWorkspace({...current,accepted:[...accepted]}); } catch(error) { check.checked=!check.checked; notice(error.message); } finally { check.disabled=false; } });
    label.append(check,node('span','',criterion)); $('acceptance-checklist').append(label);
  }
}

function businessRequirementText(text) {
  try {
    const doc=JSON.parse(text);
    if(typeof doc.title==='string' && typeof doc.business_context==='string' && Array.isArray(doc.feature_packages))
      return `${doc.title}\n${doc.business_context}\n\n功能范围：\n${doc.feature_packages.map(p=>`• ${p.name}（${p.priority}）`).join('\n')}`;
  } catch(_) {}
  return text;
}

// Polling supplements SSE, and refreshes planning changes made in another browser.
let workspacePolling=false;
setInterval(async()=>{
  if(workspacePolling || state.view!=='project-detail' || !state.project) return;
  workspacePolling=true;
  const id=state.project.id, navigation=state.navigation;
  try {
    const [project,history]=await Promise.all([api(`/v1/projects/${id}`),api(`/v1/runs?project_id=${id}`)]);
    if(navigation!==state.navigation) return;
    state.project=project; state.projectRuns=history.runs;
    if(state.current) await refreshRun(state.current.id); else renderWorkspace();
  } catch(error) { notice(error.message); } finally { workspacePolling=false; }
},10000);
