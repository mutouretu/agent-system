'use strict';
let studio=null, studioBusy=false, studioFilter='ALL', studioDirty=false, studioPoll=null;
const studioView=node('section','view hidden'); studioView.id='requirement-studio';
document.querySelector('main>footer').before(studioView);
const studioBack=node('button','quiet','← 返回项目概览'); studioBack.addEventListener('click',()=>{if(studioDirty){studioMessage.textContent='请先保存当前需求草稿。';return;}if(studio)openProject(studio.project_id);});
const studioHeading=node('div','page-heading'); const studioIdentity=node('div'); studioIdentity.append(node('div','eyebrow','REQUIREMENT STUDIO'),node('h1','','需求定义与 PRD 拆解'));
const studioActions=node('div','heading-actions');
const studioSave=node('button','secondary','保存草稿'), studioSubmit=node('button','primary','提交给 Team Lead 执行 →'); studioActions.append(studioSave,studioSubmit); studioHeading.append(studioIdentity,studioActions);
const studioScope=node('section','studio-panel requirement-scope');
const studioScopeHeading=node('div','section-heading'); studioScopeHeading.append(node('h2','','所属需求'));
const studioSelectLabel=node('label','','切换需求'); const studioSelect=node('select'); studioSelect.id='studio-requirement-select'; studioSelectLabel.append(studioSelect);
const studioNewRequirement=node('button','secondary','＋ 新建需求');
studioScopeHeading.append(studioSelectLabel,studioNewRequirement);
studioScope.append(studioScopeHeading,node('p','field-note','同一需求下保存连续对话，功能、变更与 Debug 各自作为开发项执行。'));
const studioMessage=node('p','field-note'); studioMessage.id='studio-message'; studioMessage.setAttribute('role','status');
const copilot=node('section','studio-copilot'); copilot.append(node('div','eyebrow','ARCHITECT COPILOT'),node('h2','','AI 需求助手 & 原始文本导入'));
const chat=node('div','studio-chat'); chat.id='studio-chat';
const rawLabel=node('label','','粘贴需求草案 / 向 Architect 提问'); const raw=node('textarea'); raw.id='studio-raw'; raw.rows=4; raw.maxLength=16000; raw.placeholder='粘贴 Markdown、业务需求，或说明要修改的条款…'; rawLabel.append(raw);
const copilotActions=node('div','composer-options'); const backendLabelEl=node('label','','AI 后端'); const studioBackend=node('select'); studioBackend.id='studio-backend'; for(const [value,text] of [['mixed','Codex 架构 + DeepSeek 编码审查'],['codex','Codex · 订阅'],['llm','LLM API']]){ const option=node('option','',text);option.value=value;studioBackend.append(option);} backendLabelEl.append(studioBackend);
const parseButton=node('button','primary','🤖 一键 AI 结构化拆解与回填'); copilotActions.append(backendLabelEl,parseButton); copilot.append(chat,rawLabel,copilotActions);
const studioLayout=node('div','studio-layout'), studioMain=node('div'), studioAside=node('aside','studio-aside');
const packagePanel=node('section','studio-panel'), packageHead=node('div','section-heading'); packageHead.append(node('h2','','业务功能包'));
const filters=node('div','studio-filters'); for(const [value,text] of [['ALL','全部'],['P0','P0 核心'],['OTHER','P1/P2']]){const button=node('button','secondary',text);button.dataset.filter=value;button.setAttribute('aria-pressed',String(value==='ALL'));button.addEventListener('click',()=>{studioFilter=value;renderPackages();});filters.append(button);}
const addPackage=node('button','table-action','＋ 添加功能包');addPackage.addEventListener('click',()=>{studio.document.feature_packages.push({id:'PKG-'+crypto.randomUUID().slice(0,8),name:'',description:'',priority:'P1',group:'新模块'});studioChanged();renderPackages();});packageHead.append(filters,addPackage);
const packageList=node('div');packageList.id='studio-packages';packagePanel.append(packageHead,packageList);
const criteriaPanel=node('section','studio-panel');criteriaPanel.append(node('h2','','业务验收条件'),node('p','field-note','勾选代表人工验收确认；保存后同步项目交付面板。AI 不会替你确认。'));
const criteriaList=node('div');criteriaList.id='studio-criteria'; const addCriterion=node('button','table-action','＋ 添加验收条件'); addCriterion.addEventListener('click',()=>{studio.document.acceptance_criteria.push('');studioChanged();renderCriteria();});criteriaPanel.append(criteriaList,addCriterion);
const workItemsPanel=node('section','studio-panel work-items-panel');
workItemsPanel.append(node('div','section-heading'));
workItemsPanel.querySelector('.section-heading').append(node('h2','','开发项列表'),node('span','muted','功能 / 变更 / Debug · 点击进入单项开发'));
const workItemList=node('div','work-item-list'); workItemList.id='work-item-list'; workItemsPanel.append(workItemList);
studioMain.append(workItemsPanel,leadPanel,packagePanel,criteriaPanel);
const metadataPanel=node('section','studio-panel'); const reqId=node('p','mono');reqId.id='studio-id'; metadataPanel.append(node('h2','','需求元数据'),reqId);
for(const [id,text,tag] of [['studio-title','需求名称','input'],['studio-context','核心业务目标','textarea']]){ const label=node('label','',text),field=node(tag);field.id=id;field.maxLength=id==='studio-title'?512:4000;if(tag==='textarea')field.rows=6;field.addEventListener('input',()=>{studio.document[id==='studio-title'?'title':'business_context']=field.value;studioChanged();});label.append(field);metadataPanel.append(label); }
const reviewPanel=node('section','studio-panel studio-review'); reviewPanel.append(node('h2','','Architect 智能审校'));
const score=node('strong','studio-score'), risks=node('ul'), reviewNote=node('p','field-note','填写完整度为本地规则检查；AI 审校以最近一次 Architect 回复为准。');
const boundaryButton=node('button','secondary','一键补全边界用例');reviewPanel.append(score,reviewNote,risks,boundaryButton);studioAside.append(metadataPanel,reviewPanel);studioLayout.append(studioMain,studioAside);studioView.append(studioBack,studioHeading,studioScope,studioMessage,copilot,studioLayout);
function emptyPRD(){return {title:'',business_context:'',feature_packages:[],acceptance_criteria:[]};}
function studioChanged(){studioDirty=true;studioMessage.textContent='有未保存修改';renderStudioReview();}
function setStudioBusy(value){studioBusy=value;for(const button of [studioSave,studioSubmit,studioNewRequirement,parseButton,boundaryButton,addPackage,addCriterion])button.disabled=value; studioView.querySelectorAll('input,textarea,select').forEach(field=>field.disabled=value);}
async function openRequirementEntry(projectId){
  try {const list=await api(`/v1/projects/${projectId}/requirements`);const latest=[...list.requirements].reverse().find(item=>item.kind!=='legacy');const req=latest || await api(`/v1/projects/${projectId}/requirements`,{method:'POST'});await openStudio(projectId,req.id); }catch(error){notice(error.message);}
}
async function openStudio(projectId,reqId,push=true){
  if(studioDirty && state.view==='requirement-studio') {notice('请先保存当前需求草稿再切换。');return;}
  const navigation=switchView('requirement-studio',`#requirements/${projectId}/${reqId}`,push);
  clearTimeout(studioPoll); studio=null;studioMessage.textContent='正在加载需求…';setStudioBusy(true);
  try {const [req,project,requirementList,history]=await Promise.all([api(`/v1/projects/${projectId}/requirements/${reqId}`),api(`/v1/projects/${projectId}`),api(`/v1/projects/${projectId}/requirements`),api(`/v1/runs?project_id=${projectId}`)]);if(navigation!==state.navigation)return;
    studio={...req,document:req.document || emptyPRD()};state.project=project;state.projectRuns=history.runs;state.requirements=requirementList.requirements;state.selectedRequirementId=reqId;state.selectedWorkItemId=null;state.current=null;studioDirty=false;studioFilter='ALL';raw.value='';studioBackend.value=project.context?.preferred_backend==='mixed'?'mixed':project.context?.preferred_backend==='llm'?'llm':'codex';$('workspace-backend').value=project.context?.agent_config?'configured':project.context?.preferred_backend==='llm'?'llm':project.context?.preferred_backend==='cli'?'codex':'mixed';$('breadcrumb-current').textContent='需求管理 / PRD 工作台';renderStudio();studioMessage.textContent=`已加载草稿 · 版本 ${studio.revision}`;setStudioBusy(false);
    studioSelect.replaceChildren(); for(const item of state.requirements){const option=node('option','',requirementTitleFromRecord(item));option.value=item.id;studioSelect.append(option);} studioSelect.value=reqId;
    await refreshRequirementData(reqId);
    if(req.analysis_id) pollAnalysis(req.analysis_id,navigation);
  }catch(error){if(navigation===state.navigation){studioMessage.textContent=error.message;}}
}
function renderStudio(){
  $('studio-title').value=studio.document.title;$('studio-context').value=studio.document.business_context;reqId.textContent=`REQ-${studio.id.slice(0,8)}\n${studio.id}`;
  chat.replaceChildren();for(const message of studio.messages){const card=node('article','message');card.append(node('strong','',message.role==='architect'?'Architect':'你'),node('p','message-content',message.content));chat.append(card);}if(!studio.messages.length)chat.append(node('p','muted','粘贴原始需求或提出问题。Architect 会整理条款、识别遗漏并回填结构化表单。'));
  renderPackages();renderCriteria();renderStudioReview();
}
function renderPackages(){
  packageList.replaceChildren();filters.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.filter===studioFilter)));
  const packages=studio.document.feature_packages.filter(p=>studioFilter==='ALL'||(studioFilter==='P0'?p.priority==='P0':p.priority!=='P0'));
  if(!packages.length)packageList.append(node('p','panel-empty','此筛选下暂无功能包，可手动添加或使用 AI 拆解。'));
  for(const group of [...new Set(packages.map(p=>p.group))]){
    const section=node('details','studio-module');section.open=true;section.append(node('summary','',`${group || '未命名模块'} · ${packages.filter(p=>p.group===group).length} 个功能包`));const grid=node('div','package-grid');
    for(const item of packages.filter(p=>p.group===group)){
      const card=node('article',`package-card priority-${item.priority}`);card.append(node('span',`priority-tag ${item.priority}`,`${item.priority} · ${item.id}`));
      for(const [key,title,tag] of [['name','功能名称','input'],['group','模块分组','input'],['description','功能说明','textarea'],['priority','优先级','select']]){
        const label=node('label','',title),field=node(tag);if(tag==='select')for(const priority of ['P0','P1','P2']){const option=node('option','',priority);option.value=priority;field.append(option);}else field.maxLength=key==='description'?2000:512;
        field.value=item[key];field.addEventListener('input',()=>{item[key]=field.value;studioChanged();});if(key==='priority'||key==='group')field.addEventListener('change',renderPackages);label.append(field);card.append(label);
      }
      const remove=node('button','quiet danger-text','移除此功能包');remove.addEventListener('click',()=>{studio.document.feature_packages=studio.document.feature_packages.filter(p=>p!==item);studioChanged();renderPackages();});card.append(remove);grid.append(card);
    }section.append(grid);packageList.append(section);
  }
}
function renderCriteria(){
  criteriaList.replaceChildren();studio.document.acceptance_criteria.forEach((text,index)=>{
    const row=node('div','criterion-editor'),check=node('input'),field=node('input'),remove=node('button','quiet','移除');check.type='checkbox';check.checked=studio.accepted.includes(text);check.setAttribute('aria-label',`验收确认：${text || '新条目'}`);field.value=text;field.maxLength=512;field.setAttribute('aria-label',`验收条件 ${index+1}`);
    check.addEventListener('change',()=>{studio.accepted=studio.accepted.filter(x=>x!==field.value);if(check.checked)studio.accepted.push(field.value);studioChanged();});
    field.addEventListener('input',()=>{studio.accepted=studio.accepted.filter(x=>x!==studio.document.acceptance_criteria[index]);check.checked=false;studio.document.acceptance_criteria[index]=field.value;studioChanged();});
    remove.addEventListener('click',()=>{studio.accepted=studio.accepted.filter(x=>x!==studio.document.acceptance_criteria[index]);studio.document.acceptance_criteria.splice(index,1);studioChanged();renderCriteria();});row.append(check,field,remove);criteriaList.append(row);
  });
}
function renderStudioReview(){
  if(!studio)return;const doc=studio.document,checks=[!!doc.title.trim(),!!doc.business_context.trim(),doc.feature_packages.length>0,doc.feature_packages.length>0&&doc.feature_packages.every(p=>p.name.trim()&&p.description.trim()),doc.acceptance_criteria.length>0&&doc.acceptance_criteria.every(x=>x.trim())];
  score.textContent=`${checks.filter(Boolean).length*20}/100`;risks.replaceChildren();const hints=['尚未填写需求名称','尚未说明业务目标','尚未定义功能范围','功能说明还不完整','尚未定义可验收条件'];checks.forEach((ok,i)=>{if(!ok)risks.append(node('li','',hints[i]));});
  if(studio.ai_review){risks.append(node('li','',`最近 AI 评估：${studio.ai_review.completeness_score}/100${JSON.stringify(studio.document)!==JSON.stringify(studio.ai_review.document)?'（表单已修改，待重新审校）':''}`));studio.ai_review.risks.forEach(r=>risks.append(node('li','',r)));}
}
function studioPayload(){return {revision:studio.revision,document:studio.document,accepted:studio.accepted,messages:studio.messages.slice(-30),applied_analysis_id:studio.applied_analysis_id || null,ai_review:studio.ai_review || null};}
async function saveStudio(submit=false){
  if(!studio||studioBusy)return;setStudioBusy(true);const navigation=state.navigation;
  try {if(raw.value.trim()){studio.messages.push({role:'user',content:raw.value.trim()});raw.value='';}
    const payload=studioPayload();if(submit)payload.backend=studioBackend.value;
    const result=await api(`/v1/projects/${studio.project_id}/requirements/${studio.id}${submit?'/submit':''}`,{method:submit?'POST':'PUT',body:JSON.stringify(payload)});
    if(navigation!==state.navigation)return;studio=result;studioDirty=false;renderStudio();studioMessage.textContent='草稿已保存，验收条件已同步至项目工作区。';
    if(submit){await openWorkItem(result.project_id,result.id,result.work_item_id,true,result.run_id);}
  }catch(error){if(navigation===state.navigation)studioMessage.textContent=error.message;}finally{setStudioBusy(false);}
}
studioSave.addEventListener('click',()=>saveStudio());studioSubmit.addEventListener('click',()=>saveStudio(true));
async function analyzeStudio(boundary=false){
  if(!studio||studioBusy)return;const prompt=boundary?'请在保留现有业务范围的前提下，补全边界条件与可测试的验收用例；不明确的参数请提问。':raw.value.trim();
  if(!prompt){studioMessage.textContent='请先粘贴需求草案或输入问题。';return;}setStudioBusy(true);const navigation=state.navigation;
  try {const result=await api(`/v1/projects/${studio.project_id}/requirements/${studio.id}/analyze`,{method:'POST',body:JSON.stringify({revision:studio.revision,prompt,document:studio.document,messages:studio.messages.slice(-20),backend:studioBackend.value==='mixed'?'codex':studioBackend.value})});
    if(navigation!==state.navigation)return;studio.messages.push({role:'user',content:prompt});raw.value='';studio.analysis_id=result.run_id;studioDirty=true;renderStudio();pollAnalysis(result.run_id,navigation);
  }catch(error){if(navigation===state.navigation){studioMessage.textContent=error.message;setStudioBusy(false);}}
}
async function pollAnalysis(id,navigation){
  if(navigation!==state.navigation||!studio)return;
  try {const run=await api(`/v1/runs/${id}`);if(navigation!==state.navigation)return;
    if(run.status==='COMPLETED'){
      if(studio.applied_analysis_id===id){setStudioBusy(false);return;}
      const result=run.summary.prd;if(!result)throw new Error('Architect 未返回有效结构化 PRD');
      studio.document=result.document;studio.ai_review=result;studio.accepted=[];studio.messages.push({role:'architect',content:result.reply});studio.applied_analysis_id=id;studioDirty=true;renderStudio();studioMessage.textContent='AI 拆解已回填，请审阅后保存草稿或提交执行。';setStudioBusy(false);
    }else if(['FAILED','INTERRUPTED'].includes(run.status)){throw new Error(run.summary.error||'Architect 未能完成解析，可调整需求后重试。');}
    else{setStudioBusy(true);studioMessage.textContent=run.status==='QUEUED'?'需求已排队，等待 Architect…':'Architect 正在解析与审校…';studioPoll=setTimeout(()=>pollAnalysis(id,navigation),2000);}
  }catch(error){if(navigation===state.navigation){studioMessage.textContent=error.message;setStudioBusy(false);}}
}
parseButton.addEventListener('click',()=>analyzeStudio());boundaryButton.addEventListener('click',()=>analyzeStudio(true));
studioSelect.addEventListener('change',()=>{const next=studioSelect.value;if(studioDirty){studioSelect.value=studio.id;studioMessage.textContent='请先保存当前需求草稿再切换。';return;}openStudio(studio.project_id,next);});
studioNewRequirement.addEventListener('click',async()=>{if(studioDirty){studioMessage.textContent='请先保存当前需求草稿。';return;}try{const req=await api(`/v1/projects/${studio.project_id}/requirements`,{method:'POST'});await openStudio(studio.project_id,req.id);}catch(error){studioMessage.textContent=error.message;}});
raw.addEventListener('input',studioChanged);
window.addEventListener('beforeunload',event=>{if(studioDirty&&state.view==='requirement-studio'){event.preventDefault();event.returnValue='';}});
init();
