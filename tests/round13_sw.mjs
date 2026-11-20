// Real Edge, HTTP and service workers. No browser libraries or provider calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {spawn, execFileSync} from 'node:child_process';
const root = new URL('../', import.meta.url);
let version = 'old', legacy = process.argv.includes('--legacy'), networkDown = false;
// --legacy replays an upgrade from the first service worker (commit 7a45a5b of the original history).
const oldSW = legacy ? execFileSync('git',['show','7a45a5b:web/sw.js'],{encoding:'utf8'}) : '';
const source = name => fs.readFileSync(new URL('web/' + name, root), 'utf8');
const server = http.createServer((req, res) => {
  if (networkDown) { req.socket.destroy(); return; }
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname.replace(/^\/assets\/[^/]+/, '');
  res.setHeader('Cache-Control', 'no-store');
  if (p === '/api/web-version') { res.setHeader('Content-Type','application/json'); return res.end(JSON.stringify({version})); }
  if (p === '/sw.js') { res.setHeader('Content-Type', 'text/javascript'); return res.end((legacy && version==='old' ? oldSW : source('sw.js')).replaceAll('__SNAPLAB_VERSION__', version)); }
  if (p === '/app.js') { res.setHeader('Content-Type', 'text/javascript'); return res.end(`window.runningVersion=${JSON.stringify(version)};document.body.dataset.version=window.runningVersion;`); }
  if (p === '/update.js') { res.setHeader('Content-Type','text/javascript'); return res.end(fs.existsSync(new URL('web/update.js',root)) ? source('update.js').replaceAll('__SNAPLAB_VERSION__', version) : ''); }
  if (p === '/') {
    res.setHeader('Content-Type','text/html');
    const prefix = legacy && version==='old' ? '' : '/assets/' + version;
    return res.end(`<!doctype html><meta charset=utf-8>${version==='old'?'':'<details><summary>New message</summary>New content</details>'}<textarea class=composer-input></textarea><input id=choice type=checkbox><details id=card><summary>Card</summary>Contents</details><script src="${prefix}/app.js"></script><script src="${prefix}/update.js" data-version="${version}"></script><script>navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'});</script>`);
  }
  res.setHeader('Content-Type',p.endsWith('.js')?'text/javascript':'text/html');
  res.end(p === '/offline.html' ? '<p>OFFLINE</p>' : '');
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const profile = fs.mkdtempSync(path.join(os.tmpdir(),'snaplab-sw-'));
const edge = process.env.SNAPLAB_TEST_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const proc = spawn(edge,['--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore',windowsHide:true});
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
let ws, seq=0; const pending=new Map();
async function poll(fn, seconds=20) { const end=Date.now()+seconds*1000; while(Date.now()<end) { try { const v=await fn();if(v)return v; }catch{} await delay(100); } throw Error('Timed out waiting for browser condition'); }
function cdp(method,params={}) { return new Promise((resolve,reject)=>{ const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params})); }); }
async function evaluate(expression) { const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value; }
try {
 const port=await poll(()=>fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]);
 const targets=await (await fetch(`http://127.0.0.1:${port}/json`)).json();
 ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise(resolve=>ws.addEventListener('open',resolve));
 ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p.reject(m.error);else p.resolve(m.result);}});
 await cdp('Page.enable');await cdp('Network.enable');
 await cdp('Page.navigate',{url:origin});
 await poll(()=>evaluate('navigator.serviceWorker.controller && window.runningVersion'));
 await cdp('Page.navigate',{url:origin});await poll(()=>evaluate('window.runningVersion === "old"'));
 if(legacy) {
   version='new'; await cdp('Page.navigate',{url:origin});
   await poll(()=>evaluate('!!window.runningVersion'));
   assert.equal(await evaluate('window.runningVersion'),'new','first open must execute new app.js');
 } else {
   await evaluate('document.querySelector("textarea").value="Unsent text 日本語"; document.querySelector("#card").open=true; document.querySelector("#choice").checked=true');
   const background=await cdp('Target.createTarget',{url:'about:blank'});
   await poll(()=>evaluate('document.hidden'));
   version='new';
   await cdp('Page.bringToFront');
   await poll(()=>evaluate('window.runningVersion === "new"'));
   await cdp('Target.closeTarget',{targetId:background.targetId});
   await poll(()=>evaluate('document.querySelector("textarea").value === "Unsent text 日本語"'));
   assert.equal(await evaluate('document.querySelector("#card").open'),true);
   assert.equal(await evaluate('document.querySelector("#choice").checked'),true);
   const stamp=await evaluate('performance.timeOrigin');await delay(2500);
   assert.equal(await evaluate('performance.timeOrigin'),stamp,'must not reload repeatedly');
   await evaluate('const sh=document.createElement("div");sh.className="sheet in";sh.textContent="Unsaved card";document.body.appendChild(sh)');
   version='third';await evaluate('window.dispatchEvent(new Event("focus"))');await delay(1500);
   assert.equal(await evaluate('window.runningVersion'),'new');
   assert.equal(await evaluate('document.querySelector(".sheet").textContent'),'Unsaved card');
   await evaluate('document.querySelector(".sheet").remove()');
   await poll(()=>evaluate('window.runningVersion === "third"'));
   version='fourth';await cdp('Page.navigate',{url:origin});
   await poll(()=>evaluate('window.runningVersion === "fourth"'));
   networkDown = true;
   await cdp('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
   await cdp('Page.navigate',{url:origin});await poll(()=>evaluate('document.body.textContent.includes("OFFLINE")'));
 }
 console.log('PASS: real service worker first-open freshness, automatic update, draft/card preservation, no reload loop, offline fallback');
} finally {
 ws?.close();server.closeAllConnections();server.close();
 if(proc.pid) { try {execFileSync('taskkill',['/PID',String(proc.pid),'/T','/F'],{stdio:'ignore',windowsHide:true});}catch{} }
 try {fs.rmSync(profile,{recursive:true,force:true});}catch{}
}
