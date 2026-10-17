const $ = s => document.querySelector(s);
const token = $('meta[name="workspace-token"]').content;
let snapshot, tab = 'original', mode = 'text', selected = new Set(), busy = false, previewUrl;
const requestedTab = new URLSearchParams(location.search).get('tab');
if(['original','ready','archive'].includes(requestedTab)) tab=requestedTab;
let toastTimer;
const dateFilters = {original:'all',ready:'all',archive:'all'};
function visibleItems() {return (snapshot?.items[tab] || []).filter(item => dateFilters[tab] === 'all' || item.day === dateFilters[tab]);}
function selectableItems() {return visibleItems();}
function selectVisible() {selected=new Set(selectableItems().map(item=>item.name));}
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 7000); }
async function api(path, body, binary = false) {
  const response = await fetch('/api/' + path, body === undefined ? {} : {method:'POST', headers:{'X-Workspace-Token':token,...(body instanceof FormData ? {} : {'Content-Type':'application/json'})},body:body instanceof FormData ? body : JSON.stringify(body)});
  if (!response.ok) { let error; try {error = (await response.json()).error;} catch {error = '操作未完成，请刷新后重试。';} throw new Error(error); }
  return binary ? response.blob() : response.json();
}
function options() {return {mode,text:$('#watermark-text').value,position:$('#position').value,size:Number($('#size').value),opacity:Number($('#opacity').value),color:$('#color').value};}
function applyOptions(value) {
  mode = value.mode;
  $('#watermark-text').value = value.text; $('#position').value = value.position; $('#size').value = value.size; $('#opacity').value = value.opacity; $('#color').value = value.color;
  renderOptions();
}
function renderOptions() {
  document.querySelectorAll('[data-mode]').forEach(button => button.classList.toggle('active',button.dataset.mode === mode));
  $('#text-fields').hidden = mode !== 'text'; $('#logo-fields').hidden = mode !== 'logo';
  $('#size-value').textContent = $('#size').value + '%'; $('#opacity-value').textContent = $('#opacity').value + '%';
}
async function refresh(reset = false) {
  snapshot = await api('state');
  if(dateFilters[tab] !== 'all' && !snapshot.items[tab].some(item=>item.day===dateFilters[tab])) dateFilters[tab]='all';
  if(reset) selectVisible();
  else selected = new Set([...selected].filter(name => selectableItems().some(item => item.name === name)));
  $('#folder-notice').hidden = Object.keys(snapshot.settings.folders).length === 3;
  $('#error-notice').textContent = snapshot.errors.join('\n'); $('#error-notice').hidden = !snapshot.errors.length;
  $('#today').textContent = new Date(snapshot.today + 'T12:00:00').toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
  $('#auto-archive').checked = snapshot.settings.auto_archive;
  if(snapshot.logo) $('#logo-status').textContent = '已上传 Logo · 点击可替换';
  renderEnvironment(snapshot.environment);
  renderJobs();
  render();
}
function syncSelection() {
  const selectable = selectableItems();
  $('#selection-count').textContent = `已选 ${selected.size} 张`;
  $('#apply-count').textContent = selected.size;
  $('#select-all').checked = selectable.length > 0 && selectable.every(item => selected.has(item.name));
  $('#select-all').indeterminate = selected.size > 0 && !$('#select-all').checked;
  for(const id of ['apply','preview','move','restore']) $('#' + id).disabled = busy || selected.size === 0;
  $('#select-all').disabled = busy;
  $('#engine').disabled = busy;
  document.querySelectorAll('.image-card').forEach(card => {card.classList.toggle('selected',selected.has(card.dataset.name));card.querySelector('input').checked=selected.has(card.dataset.name);});
}
function render() {
  document.querySelectorAll('[data-tab]').forEach(button => {button.classList.toggle('active',button.dataset.tab === tab);button.querySelector('span').textContent=snapshot.items[button.dataset.tab].length;});
  $('#current-path').textContent = snapshot.settings.folders[tab] || '尚未选择文件夹'; $('#current-path').title=$('#current-path').textContent;
  $('#watermark-panel').hidden=tab !== 'original'; $('#archive-panel').hidden=tab === 'original'; $('#move').hidden=tab !== 'ready'; $('#restore').hidden=tab !== 'archive';
  $('#archive-title').textContent=tab === 'archive' ? '随时找回好作品' : '整理完成的图片';
  $('#archive-description').textContent=tab === 'archive' ? '选中图片可移回第二栏，按今天重新计时。' : '发布完成，或今天暂时不用？选中后即可归档。';
  $('#gallery').replaceChildren();
  renderDates();
  const items=visibleItems().sort((a,b)=>b.day.localeCompare(a.day));
  let lastDay='';
  for(const item of items) {
    if(item.day !== lastDay) {const heading=document.createElement('div'); heading.className='day-heading';heading.textContent=(item.day===snapshot.today?'今天':item.day);$('#gallery').append(heading);lastDay=item.day;}
    const card=document.createElement('article');card.className='image-card';card.dataset.name=item.name;
    const wrap=document.createElement('div');wrap.className='image-wrap';
    const image=document.createElement('img');image.src=`/api/image/${tab}/${item.name.split('/').map(encodeURIComponent).join('/')}?thumb=1`;image.loading='lazy';image.alt=item.filename;
    image.onerror=()=>{image.alt='图片无法预览：'+item.filename;};
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.setAttribute('aria-label','选择 '+item.filename);
    wrap.append(image,checkbox);
    wrap.addEventListener('click',event=>{if(busy||checkbox.disabled)return;if(selected.has(item.name))selected.delete(item.name);else selected.add(item.name);syncSelection();});
    checkbox.addEventListener('click',event=>event.stopPropagation());
    checkbox.addEventListener('change',()=>{if(checkbox.checked)selected.add(item.name);else selected.delete(item.name);syncSelection();});
    const filename=document.createElement('div');filename.className='filename';filename.textContent=item.filename;filename.title=item.name;
    const meta=document.createElement('div');meta.className='meta';meta.textContent=`${(item.bytes/1024/1024).toFixed(2)} MB · ${item.timestamp?new Date(item.timestamp).toLocaleString('zh-CN',{hour12:false}):item.day}`;
    card.append(wrap,filename,meta);$('#gallery').append(card);
  }
  $('#empty').hidden=items.length>0;
  $('#empty-description').textContent=tab==='original'?'把图片放入原图文件夹，再点击刷新。':tab==='ready'?'在「未加水印」里选图，添加你的签名。':'自动归档和手动归档的图片会出现在这里。';
  syncSelection();
}
function renderDates() {
  const container=$('#date-filter');container.replaceChildren();
  const counts=new Map();for(const item of snapshot.items[tab])counts.set(item.day,(counts.get(item.day)||0)+1);
  const dates=[['all',snapshot.items[tab].length],...[...counts.entries()].sort((a,b)=>b[0].localeCompare(a[0]))];
  for(const [day,count] of dates) {
    const button=document.createElement('button');button.dataset.date=day;button.className='date-chip';button.classList.toggle('active',dateFilters[tab]===day);button.setAttribute('aria-pressed',dateFilters[tab]===day?'true':'false');
    button.textContent=`${day==='all'?'全部日期':day} · ${count}`;
    button.onclick=()=>{if(busy)return;dateFilters[tab]=day;selectVisible();render();};container.append(button);
  }
}
function renderJobs() {
  const jobs=snapshot.jobs||[];$('#codex-jobs').hidden=!jobs.length;$('#job-list').replaceChildren();
  const labels={waiting:'等待成品',running:'Codex 正在改图',partial:'部分已导入',completed:'已完成并移动',failed:'处理失败'};
  for(const job of jobs) {
    const card=document.createElement('div');card.className='job-card';
    const title=document.createElement('strong');title.textContent=`${labels[job.status]||job.status} · ${Object.keys(job.imported||{}).length}/${job.count} 张`;
    const time=document.createElement('small');time.textContent=job.created?.replace('T',' ');
    const open=document.createElement('button');open.className='quiet';open.textContent='打开任务文件夹 ↗';open.onclick=()=>api('codex/open',{id:job.id}).catch(error=>toast(error.message));
    card.append(title,time,open);
    if(job.progress){const progress=document.createElement('p');progress.className='footnote';progress.textContent=job.progress;card.append(progress);}
    if(['failed','waiting','partial'].includes(job.status)) {
      const retry=document.createElement('button');retry.className='secondary full';retry.textContent='自动执行 / 重试';retry.onclick=()=>action(retry,async()=>{await api('codex/retry',{id:job.id});await refresh(false);toast('已自动提交任务。');});card.append(retry);
    }
    if(job.error){const error=document.createElement('p');error.className='inline-error';error.textContent=job.error;card.append(error);}
    $('#job-list').append(card);
  }
}
function renderEngine() {
  const creative=$('#engine').value==='codex';$('#creative-fields').hidden=!creative;$('#preview').hidden=creative;
  $('#apply-label').textContent=creative?'自动执行 Codex 改图':'直接加水印';
  $('#engine-note').textContent=creative?'点击后通过本机 CLI 自动提交任务，无需粘贴。成功后移入第二栏，失败保留待处理图片。':'成功后移入“加水印”，从待处理文件夹移除；失败的图片留在原处。';
  if(snapshot){selectVisible();render();}
}
function renderEnvironment(environment) {
  if(!environment)return;
  $('#codex-status').textContent=environment.error || `${environment.version} · 已登录 · 生图已启用。选中图片将交给 Codex 服务。`;
  $('#codex-status').classList.toggle('inline-error',Boolean(environment.error));
}
$('#check-environment').onclick=()=>action($('#check-environment'),async()=>{snapshot.environment=await api('environment',{});renderEnvironment(snapshot.environment);toast(snapshot.environment.error||'Codex 环境检查通过，可自动执行。');});
async function action(button, operation) {
  if(busy)return;
  busy=true;button.disabled=true;button.setAttribute('aria-busy','true');syncSelection();toast('正在处理，请稍候…');
  try {await operation();} catch(error) {toast(error.message);} finally {busy=false;button.disabled=false;button.removeAttribute('aria-busy');syncSelection();}
}
function openSettings() {for(const kind of ['original','ready','archive']) $('#folder-'+kind).value=snapshot?.settings.folders[kind]||'';$('#settings-error').textContent='';$('#settings-dialog').showModal();}
$('#settings-button').onclick=openSettings;$('#setup-button').onclick=openSettings;
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$('#'+button.dataset.close).close());
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{if(busy)return;tab=button.dataset.tab;selectVisible();render();});
$('#engine').onchange=renderEngine;
document.querySelectorAll('[data-mode]').forEach(button=>button.onclick=()=>{mode=button.dataset.mode;renderOptions();});
$('#size').oninput=renderOptions;$('#opacity').oninput=renderOptions;
$('#select-all').onchange=()=>{if(busy)return;if($('#select-all').checked)selectVisible();else selected=new Set();syncSelection();};
$('#refresh').onclick=()=>action($('#refresh'),()=>refresh(true));
async function openFolder() {try{await api('open-folder',{kind:tab});}catch(error){toast(error.message);}}
$('#open-folder').onclick=openFolder;$('#empty-folder').onclick=openFolder;
document.querySelectorAll('[data-pick]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{const result=await api('pick-folder',{});if(result.path)$('#folder-'+button.dataset.pick).value=result.path;}catch(error){$('#settings-error').textContent=error.message;}finally{button.disabled=false;}});
$('#settings-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const folders={};for(const kind of ['original','ready','archive'])folders[kind]=$('#folder-'+kind).value;await api('settings',{folders});$('#settings-dialog').close();await refresh(true);toast('文件夹设置已保存');}catch(error){$('#settings-error').textContent=error.message;}finally{button.disabled=false;}};
$('#auto-archive').onchange=async()=>{const input=$('#auto-archive');input.disabled=true;try{await api('settings',{auto_archive:input.checked});toast(input.checked?'已开启：跨天后自动归档':'已关闭自动归档');}catch(error){input.checked=!input.checked;toast(error.message);}finally{input.disabled=false;}};
$('#logo-upload').onchange=async()=>{const file=$('#logo-upload').files[0];if(!file)return;try{const form=new FormData();form.append('file',file);await api('logo',form);$('#logo-status').textContent='已上传：'+file.name;toast('Logo 已保存在本机');}catch(error){toast(error.message);}};
$('#preview').onclick=()=>action($('#preview'),async()=>{const blob=await api('preview',{name:[...selected][0],watermark:options()},true);if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('#preview-image').src=previewUrl;$('#preview-dialog').showModal();});
$('#apply').onclick=()=>action($('#apply'),async()=>{
  if($('#engine').value==='codex') {
    const result=await api('codex/start',{names:[...selected],watermark:options(),creative:$('#creative').value});await refresh(false);
    toast(`已自动提交 ${result.count} 张图片，Codex 正在后台改图，完成后自动移动。`);
  }
  else {const result=await api('watermark',{names:[...selected],watermark:options()});await refresh(true);toast(`已完成 ${result.count} 张水印图片。`+(result.errors.length?'\n'+result.errors.join('\n'):''));}
});
for(const [id,endpoint,label] of [['move','archive','归档'],['restore','restore','移回']]) $('#'+id).onclick=()=>action($('#'+id),async()=>{const result=await api(endpoint,{names:[...selected]});await refresh(true);toast(`已${label} ${result.count} 张图片。`+(result.errors.length?'\n'+result.errors.join('\n'):''));});
async function boot() {try{await refresh(true);applyOptions(snapshot.settings.watermark);if(Object.keys(snapshot.settings.folders).length!==3)openSettings();}catch(error){toast(error.message);}}
boot();setInterval(()=>{if(!busy&&!$('#settings-dialog').open)refresh(false).catch(error=>toast('无法连接工作台，请检查启动窗口。'));},5000);
