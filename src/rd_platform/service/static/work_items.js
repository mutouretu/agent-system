'use strict';

const workItemState={items:[],detail:null,checked:new Set(),checkedRunId:null,poll:null,busy:false};
const workItemLabels={IN_PROGRESS:'开发中',AWAITING_ACCEPTANCE:'待你验收',NEEDS_ATTENTION:'需处理',DELIVERED:'已交付'};
const workItemTime=value=>new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

function renderWorkItemList(){
  const project=$('work-items-project').value,status=$('work-items-status').value,query=$('work-items-search').value.trim().toLocaleLowerCase();
  const items=workItemState.items.filter(item=>(!project||item.project_id===project)&&(!status||item.status===status)&&
    `${item.title} ${item.id} ${item.project_name} ${item.requirement_title||''}`.toLocaleLowerCase().includes(query));
  $('work-items-count').textContent=`${items.length} / ${workItemState.items.length}`;
  $('work-items-empty').classList.toggle('hidden',items.length>0);
  $('work-items-list').replaceChildren();
  for(const item of items){
    const card=node('article','work-item-row'),identity=node('div'),meta=node('div','work-item-row-meta');
    const title=node('button','work-item-row-title',item.title);title.addEventListener('click',()=>openWorkItem(item.id));
    identity.append(title,node('small','mono',`会话 ${item.id.slice(0,8)} · ${item.project_name}`));
    meta.append(node('span',`badge ${item.status}`,workItemLabels[item.status]||item.status),
      node('small','muted',`${item.run_ids.length} 次执行 · ${workItemTime(item.updated_at)}`));
    card.append(identity,meta);$('work-items-list').append(card);
  }
}

async function openWorkItems(push=true){
  const navigation=switchView('work-items-view','#work-items',push);
  $('breadcrumb-current').textContent='工作项';notice('');
  try{
    const data=await api('/v1/work-items');if(navigation!==state.navigation)return;
    workItemState.items=data.work_items;
    const selected=$('work-items-project').value;
    $('work-items-project').replaceChildren();const all=node('option','','全部项目');all.value='';$('work-items-project').append(all);
    const projects=new Map(data.work_items.map(item=>[item.project_id,item.project_name]));
    for(const [id,name] of projects){const option=node('option','',name);option.value=id;$('work-items-project').append(option);}
    $('work-items-project').value=projects.has(selected)?selected:'';
    renderWorkItemList();
  }catch(error){if(navigation===state.navigation)notice(error.message);}
}

function renderWorkItem(){
  const item=workItemState.detail;if(!item)return;
  if(workItemState.checkedRunId!==item.latest_run_id){workItemState.checked.clear();workItemState.checkedRunId=item.latest_run_id;}
  $('work-item-title').textContent=item.title;
  $('work-item-identity').textContent=`${item.project_name}${item.requirement_title?' / '+item.requirement_title:''} · 会话 ID ${item.id}`;
  $('work-item-status').className=`badge ${item.status}`;
  $('work-item-status').textContent=workItemLabels[item.status]||item.status;
  $('work-item-parent').textContent=`所属项目：${item.project_name}${item.requirement_title?' · 需求：'+item.requirement_title:''}`;
  $('work-item-description').textContent=item.description;
  $('work-item-criteria').replaceChildren();
  if(!item.acceptance_criteria.length)$('work-item-criteria').append(node('li','muted','此工作项未单独登记验收条件。'));
  for(const criterion of item.acceptance_criteria){
    const label=node('label','work-item-criterion'),check=node('input'),text=node('span','',criterion);
    check.type='checkbox';check.checked=item.status==='DELIVERED'||workItemState.checked.has(criterion);
    check.disabled=item.status==='DELIVERED';
    check.addEventListener('change',()=>{if(check.checked)workItemState.checked.add(criterion);else workItemState.checked.delete(criterion);updateWorkItemActions();});
    label.append(check,text);$('work-item-criteria').append(label);
  }
  const tests=item.latest_test_summary;
  $('work-item-test').textContent=tests?.outcome==='PASSED'?
    `最新运行的沙箱测试通过：${tests.counts?.passed??0}/${tests.total??0}。请审阅交付物并确认业务验收。`:
    item.latest_run_status==='FAILED'?'最新运行失败；可查看下方证据并从失败节点恢复。':'测试结果将在运行完成后显示。';
  $('work-item-messages').replaceChildren();
  for(const entry of item.timeline){
    const card=node('article',`work-item-message ${entry.speaker==='你'?'from-user':''}`);
    const heading=node('div','work-item-message-heading');heading.append(node('strong','',entry.speaker),node('small','mono',workItemTime(entry.time)));
    card.append(heading,node('p','',entry.text));$('work-item-messages').append(card);
  }
  $('work-item-runs').replaceChildren();
  for(const run of item.runs){
    const card=node('article','work-item-run'),heading=node('div','work-item-run-heading');
    heading.append(node('strong','',`运行 ${run.id.slice(0,8)}`),node('span',`badge ${run.status}`,labels[run.status]||run.status),node('small','mono',workItemTime(run.created_at)));
    card.append(heading);
    const summary=run.summary.final_test_summary;
    if(summary)card.append(node('p','muted',`沙箱测试 ${summary.counts?.passed??0}/${summary.total??0} 通过 · ${summary.outcome}`));
    if(run.summary.error)card.append(node('p','field-error',String(run.summary.error).slice(0,600)));
    const actions=node('div','work-item-run-actions'),artifacts=node('button','table-action','查看交付物');
    artifacts.addEventListener('click',async()=>{
      artifacts.disabled=true;try{const data=await api(`/v1/runs/${run.id}/artifacts`);const disclosure=node('details','work-item-artifacts');disclosure.open=true;
        disclosure.append(node('summary','',`${data.artifacts.length} 个产物`));
        for(const artifact of data.artifacts){const file=node('details');file.append(node('summary','',artifact.path),node('pre','',artifact.content));disclosure.append(file);}
        card.append(disclosure);artifacts.remove();}catch(error){notice(error.message);artifacts.disabled=false;}
    });actions.append(artifacts);
    if(['FAILED','INTERRUPTED'].includes(run.status)){
      const resume=node('button','secondary','从失败节点继续');resume.addEventListener('click',async()=>{
        resume.disabled=true;try{await api(`/v1/runs/${run.id}/resume`,{method:'POST',body:JSON.stringify({})});await refreshWorkItem();}
        catch(error){notice(error.message);}finally{resume.disabled=false;}
      });actions.append(resume);
    }
    card.append(actions);$('work-item-runs').append(card);
  }
  updateWorkItemActions();
}

function updateWorkItemActions(){
  const item=workItemState.detail;
  $('work-item-accept').disabled=workItemState.busy||!item||item.status!=='AWAITING_ACCEPTANCE'||
    item.acceptance_criteria.some(text=>!workItemState.checked.has(text));
  $('work-item-rework-submit').disabled=workItemState.busy||!item||item.status==='IN_PROGRESS';
  $('work-item-send').disabled=workItemState.busy||!item;
}

async function refreshWorkItem(){
  const id=workItemState.detail?.id;if(!id||state.view!=='work-item-detail')return;
  const navigation=state.navigation;
  try{const item=await api(`/v1/work-items/${id}`);if(navigation!==state.navigation)return;
    workItemState.detail=item;renderWorkItem();}
  catch(error){if(navigation===state.navigation)notice(error.message);}
}

async function openWorkItem(id,push=true){
  const navigation=switchView('work-item-detail',`#work-item/${id}`,push);
  $('breadcrumb-current').textContent='工作项 / 开发会话';$('work-item-title').textContent='加载工作项…';notice('');
  clearInterval(workItemState.poll);workItemState.detail=null;workItemState.checked.clear();workItemState.checkedRunId=null;
  try{const item=await api(`/v1/work-items/${id}`);if(navigation!==state.navigation)return;
    workItemState.detail=item;renderWorkItem();
    workItemState.poll=setInterval(()=>{if(state.view==='work-item-detail')refreshWorkItem();else clearInterval(workItemState.poll);},4000);
  }catch(error){if(navigation===state.navigation){$('work-item-title').textContent='工作项加载失败';notice(error.message);}}
}

async function loadCreateRequirements(projectId,preferredId=null){
  const select=$('work-item-create-requirement');select.replaceChildren();select.disabled=true;
  if(!projectId)return;
  const [data,project]=await Promise.all([api(`/v1/projects/${projectId}/requirements`),api(`/v1/projects/${projectId}`)]);
  for(const req of data.requirements){const option=node('option','',req.document?.title||`未命名需求 · ${req.id.slice(0,8)}`);option.value=req.id;select.append(option);}
  select.disabled=!data.requirements.length;
  select.value=data.requirements.some(req=>req.id===preferredId)?preferredId:data.requirements[0]?.id||'';
  $('work-item-create-backend').value=project.context?.agent_config?.revision>0?'configured':'codex';
  $('work-item-create-error').textContent=data.requirements.length?'':'此项目尚无需求，请先在需求管理中创建需求。';
}

async function openCreateWorkItem(projectId=null,requirementId=null){
  $('work-item-create-error').textContent='';
  try{
    const data=await api('/v1/projects');const select=$('work-item-create-project');select.replaceChildren();
    for(const project of data.projects){const option=node('option','',project.name);option.value=project.id;select.append(option);}
    if(!data.projects.length){notice('请先创建项目。');return;}
    select.value=data.projects.some(project=>project.id===projectId)?projectId:data.projects[0].id;
    await loadCreateRequirements(select.value,requirementId);
    $('work-item-create-dialog').showModal();
  }catch(error){notice(error.message);}
}

$('nav-work-items').addEventListener('click',()=>openWorkItems());
$('work-item-create').addEventListener('click',()=>openCreateWorkItem());
$('work-item-create-close').addEventListener('click',()=>$('work-item-create-dialog').close());
$('work-item-create-cancel').addEventListener('click',()=>$('work-item-create-dialog').close());
$('work-item-create-project').addEventListener('change',()=>loadCreateRequirements($('work-item-create-project').value).catch(error=>{$('work-item-create-error').textContent=error.message;}));
$('work-item-create-form').addEventListener('submit',async event=>{
  event.preventDefault();const criteria=$('work-item-create-criteria').value.split('\n').map(text=>text.trim()).filter(Boolean);
  const button=$('work-item-create-submit');button.disabled=true;$('work-item-create-error').textContent='';
  try{
    const payload={project_id:$('work-item-create-project').value,requirement_id:$('work-item-create-requirement').value,
      kind:$('work-item-create-kind').value,title:$('work-item-create-title').value.trim(),
      description:$('work-item-create-description').value.trim(),acceptance_criteria:criteria,
      backend:$('work-item-create-backend').value,workspace_mode:'repository',max_retries:3};
    const result=await api('/v1/work-items',{method:'POST',body:JSON.stringify(payload)});
    $('work-item-create-dialog').close();$('work-item-create-form').reset();await openWorkItem(result.work_item_id);
  }catch(error){$('work-item-create-error').textContent=error.message;}
  finally{button.disabled=false;}
});
$('work-item-back').addEventListener('click',()=>openWorkItems());
$('work-items-refresh').addEventListener('click',()=>openWorkItems(false));
for(const id of ['work-items-project','work-items-status','work-items-search'])$(id).addEventListener(id==='work-items-search'?'input':'change',renderWorkItemList);
$('work-item-chat-form').addEventListener('submit',async event=>{
  event.preventDefault();const item=workItemState.detail,message=$('work-item-message').value.trim();if(!item||!message||workItemState.busy)return;
  workItemState.busy=true;updateWorkItemActions();$('work-item-chat-error').textContent='';
  try{await api(`/v1/runs/${item.id}/chat`,{method:'POST',body:JSON.stringify({message})});
    $('work-item-message').value='';await refreshWorkItem();}
  catch(error){$('work-item-chat-error').textContent=error.message;}
  finally{workItemState.busy=false;updateWorkItemActions();}
});
$('work-item-accept').addEventListener('click',async()=>{
  const item=workItemState.detail;if(!item||workItemState.busy)return;
  workItemState.busy=true;updateWorkItemActions();
  try{const accepted=await api(`/v1/work-items/${item.id}/accept`,{method:'POST',body:JSON.stringify({revision:item.revision,run_id:item.latest_run_id,criteria_confirmed:[...workItemState.checked]})});
    workItemState.detail=accepted;renderWorkItem();notice('交付已由你确认。');}
  catch(error){notice(error.message);}finally{workItemState.busy=false;updateWorkItemActions();}
});
$('work-item-rework-form').addEventListener('submit',async event=>{
  event.preventDefault();const item=workItemState.detail,instruction=$('work-item-rework').value.trim();if(!item||!instruction||workItemState.busy)return;
  workItemState.busy=true;updateWorkItemActions();$('work-item-rework-error').textContent='';
  try{await api(`/v1/work-items/${item.id}/runs`,{method:'POST',body:JSON.stringify({instruction})});
    $('work-item-rework').value='';await refreshWorkItem();}
  catch(error){$('work-item-rework-error').textContent=error.message;}
  finally{workItemState.busy=false;updateWorkItemActions();}
});
