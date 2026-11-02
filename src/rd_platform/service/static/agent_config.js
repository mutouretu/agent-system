'use strict';
const agentRoleNames={ARCHITECT:'架构师',CODER:'开发者',REVIEWER:'审查者',TESTER:'测试者'};
const agentRoleOrder=['ARCHITECT','CODER','REVIEWER','TESTER'];

function renderAgentConfig() {
  const box=$('agent-config-cards'); box.replaceChildren();
  const config=state.agentConfig;
  if(!config) { box.append(node('p','muted','正在加载角色配置…')); return; }
  for(const role of agentRoleOrder) {
    const spec=config.agents[role];
    const card=node('article','agent-config-card');
    const title=node('strong','',`${agentRoleNames[role]} · ${role}`);
    const backend=node('span','agent-config-backend',spec.backend==='codex'?'Codex CLI':spec.backend==='llm'?'LLM API':'Docker 沙箱');
    const model=node('div','mono',spec.model && spec.model!=='default'?spec.model:spec.backend==='sandbox'?'不调用模型':'订阅默认模型');
    const duty=node('p','',spec.responsibility);
    card.append(title,backend,model,duty); box.append(card);
  }
  $('agent-config-message').textContent=config.revision ? `已保存 · 配置版本 ${config.revision}。新运行将使用此版本。` : '当前为默认模板，编辑并保存后才会用于新运行。';
}

function openAgentConfig() {
  if(!state.project || !state.agentConfig) return;
  const fields=$('agent-config-fields'); fields.replaceChildren();
  for(const role of agentRoleOrder) {
    const spec=state.agentConfig.agents[role], group=node('fieldset','agent-config-role');
    group.dataset.role=role; group.append(node('legend','',`${agentRoleNames[role]} · ${role}`));
    const backendLabel=node('label','','运行方式'), backend=node('select'); backend.name='backend';
    for(const [value,label] of role==='TESTER'?[['sandbox','Docker 沙箱']]:[['codex','Codex CLI'],['llm','LLM API']]) {
      const option=node('option','',label); option.value=value; backend.append(option);
    }
    backend.value=spec.backend;
    const modelLabel=node('label','','模型'), model=node('input'); model.name='model'; model.maxLength=128;
    model.value=spec.model && spec.model!=='default'?spec.model:'';
    model.placeholder=backend.value==='codex'?'留空使用订阅默认模型':'填写模型名称'; model.disabled=role==='TESTER';
    backend.addEventListener('change',()=>{ model.disabled=role==='TESTER'; model.placeholder=backend.value==='codex'?'留空使用订阅默认模型':'填写模型名称'; });
    const dutyLabel=node('label','','职责'), duty=node('textarea'); duty.name='responsibility'; duty.maxLength=1000; duty.rows=2; duty.required=true; duty.value=spec.responsibility;
    backendLabel.append(backend); modelLabel.append(model); dutyLabel.append(duty);
    group.append(backendLabel,modelLabel,dutyLabel); fields.append(group);
  }
  $('agent-config-error').textContent=''; $('agent-config-dialog').showModal();
}

$('agent-config-edit').addEventListener('click',openAgentConfig);
$('agent-config-close').addEventListener('click',()=>$('agent-config-dialog').close());
$('agent-config-cancel').addEventListener('click',()=>$('agent-config-dialog').close());
$('agent-config-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if(!state.project || !state.agentConfig) return;
  const id=state.project.id, revision=state.agentConfig.revision, agents={};
  for(const group of $('agent-config-fields').querySelectorAll('fieldset')) {
    const role=group.dataset.role, backend=group.querySelector('[name="backend"]').value;
    const rawModel=group.querySelector('[name="model"]').value.trim();
    const previous=state.agentConfig.agents[role];
    agents[role]={backend,responsibility:group.querySelector('[name="responsibility"]').value.trim(),
      model:backend==='sandbox'?null:rawModel || (backend==='codex'?'default':''),
      tools:previous.tools,credential_ref:previous.credential_ref};
  }
  const button=$('agent-config-form').querySelector('button[type="submit"]'); button.disabled=true;
  try {
    const saved=await api(`/v1/projects/${id}/agents/config`,{method:'PUT',body:JSON.stringify({revision,agents})});
    if(state.project?.id===id) {
      state.agentConfig=saved; renderAgentConfig(); $('workspace-backend').value='configured';
    }
    $('agent-config-dialog').close();
  } catch(error) { $('agent-config-error').textContent=error.message; }
  finally { button.disabled=false; }
});
