(() => {
  const $ = id => document.getElementById(id);
  let lang = window.snapBrowserLang();
  const adding = location.hash === '#add';
  let catalog = [];
  let endpointVersion = 0;
  function clearEndpointKey() {
    endpointVersion++;
    $('api-key').value = '';
    catalog = [];
    $('model-list').hidden = true;
  }
  $('base-url').addEventListener('input', clearEndpointKey);
  const labels = {'setup-title':'title', 'setup-help':'help', 'provider-label':'provider', 'url-label':'url',
    'model-label':'model', 'key-label':'key', 'setup-privacy':'privacy', 'test-provider':'continue', 'snapscribe-hint':'snapscribe',
    'edit-url':'editurl'};
  const t = key => window.snapT(lang, key);
  $('setup-language').replaceChildren(...window.snapLangs().map(item => {
    const option = document.createElement('option'); option.value = item.code; option.textContent = item.name; return option;
  }));
  function translate() {
    document.documentElement.lang = lang; document.title = t('setup.title') + ' · SnapLab';
    for (const [id,key] of Object.entries(labels)) $(id).textContent = t('setup.' + key);
    $('language-label').textContent = t('settings.lang');
    $('provider-custom').textContent = t('setup.custom');
    $('model').placeholder = t('setup.model.ph');
    $('compatibility-label').textContent=t('providers.compatibility');
    for (const [id,key] of Object.entries({'load-models':'providers.load','context-label':'providers.context','tools-label':'providers.tools','vision-label':'providers.vision','usage-label':'providers.usage','token-label':'providers.tokens','provider-notice':'providers.notice'})) $(id).textContent=t(key);
    if (adding) { $('setup-title').textContent=t('providers.add'); $('test-provider').textContent=t('providers.save'); }
    const options=$('model-list').options;
    if(options.length) options[0].textContent=t('providers.choose');
    for(let i=1;i<options.length;i++) options[i].textContent=options[i].value+(options[i].disabled?' · '+t('providers.unsupported'):'');
    window.snapRememberLang(lang);
  }
  $("setup-language").value = lang;
  $("setup-language").onchange = () => { lang = $("setup-language").value; translate(); };
  translate();
  // Presets come from the server (snaplab/presets.py, config.json or environment), so an address can change
  // without a release. A fixed preset (Snapscribe 1) shows only the key; its address stays editable on request.
  let presets = {};
  let urlOpen = false;
  function showFields() {
    const fixed = !!(presets[$("provider").value] || {}).fixed_model;
    for (const id of ["model-label", "model", "load-models"]) $(id).hidden = fixed;
    for (const id of ["url-label", "base-url"]) $(id).hidden = fixed && !urlOpen;
    $("edit-url").hidden = !fixed || urlOpen;
    if (fixed) $("model-list").hidden = true;
    $("snapscribe-hint").hidden = $("provider").value !== "snapscribe";
  }
  function applyPreset() {
    const p = presets[$("provider").value] || {base_url:"", model:"", context_limit:32768, vision:false};
    $("base-url").value = p.base_url || "";
    $("model").value = p.model || "";
    $("context-limit").value = p.context_limit || 32768;
    $("supports-vision").checked = !!p.vision;
    $("model-list").hidden = true;
    showFields();
  }
  $("provider").onchange = () => { clearEndpointKey(); urlOpen = false; applyPreset(); };
  $("edit-url").onclick = () => { urlOpen = true; showFields(); $("base-url").focus(); };
  fetch("/api/setup/presets", {headers:{"X-SnapLab":"1"}}).then(r => r.ok ? r.json() : Promise.reject())
    .then(result => {
      presets = result.presets || {};
      for (const option of $("provider").options) if (presets[option.value] && option.value !== "custom") option.textContent = presets[option.value].label;
      applyPreset();
    })
    .catch(() => { urlOpen = true; showFields(); });
  showFields();
  const payloadFor = () => ({provider:$("provider").value,base_url:$("base-url").value,model:$("model").value,key:$("api-key").value,
    context_limit:Number($("context-limit").value),tools:$("supports-tools").checked,vision:$("supports-vision").checked,
    stream_usage:$("stream-usage").checked,token_parameter:$("token-parameter").value});
  $("load-models").onclick = async () => {
    const version=endpointVersion;
    $("load-models").disabled=true;
    const payload=payloadFor();
    try {
      const response=await fetch('/api/providers/models',{method:'POST',headers:{'Content-Type':'application/json','X-SnapLab':'1'},body:JSON.stringify(payload)});
      payload.key=''; const result=await response.json(); if(!response.ok) throw Error();
      if(version!==endpointVersion)return;
      catalog=result.models; const select=$("model-list");select.replaceChildren();
      const first=document.createElement('option');first.value='';first.textContent=t('providers.choose');select.append(first);
      for(const model of catalog){const option=document.createElement('option');option.value=model.id;option.disabled=!model.supported;option.textContent=model.id+(model.supported?'':' - '+t('providers.unsupported'));select.append(option);}
      select.hidden=false; $("setup-status").textContent='';
    } catch { if(version===endpointVersion)$("setup-status").textContent=t('providers.listfailed'); }
    finally {payload.key='';$("load-models").disabled=false;}
  };
  $("model-list").onchange=()=>{const m=catalog.find(m=>m.id===$("model-list").value);if(!m)return;$("model").value=m.id;$("context-limit").value=m.context_limit;$("supports-tools").checked=m.tools;$("supports-vision").checked=m.vision;};
  $("provider-form").onsubmit = async event => {
    event.preventDefault();
    const button = $("test-provider"), status = $("setup-status");
    button.disabled = true;
    status.textContent = t("setup.testing");
    const payload = payloadFor();
    $("api-key").value = "";
    try {
      const response = await fetch(adding ? "/api/providers" : "/api/setup/provider", {method:"POST",headers:{"Content-Type":"application/json","X-SnapLab":"1","X-SnapLab-Language":lang},body:JSON.stringify(payload)});
      payload.key = "";
      const result = await response.json();
      if (!response.ok) { const error=new Error('Provider setup failed');error.code=result.error_code;throw error; }
      location.replace(adding ? "/#/settings" : "/");
    } catch (error) {
      payload.key = "";
      status.textContent = error.code ? t("err.code."+error.code) : t("setup.failed");
      button.disabled = false;
    }
  };
})();
