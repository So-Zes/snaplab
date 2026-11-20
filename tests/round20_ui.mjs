import assert from 'node:assert/strict';
import {makeUI} from './ui_harness.mjs';
const {ui}=makeUI();
const message={id:'m-result',role:'system',session:'s-example',
 text:'Work finished\nInternal result summary',result:{status:'done'},
 attachments:[{id:'f-'+'a'.repeat(32),name:'screenshot.png',kind:'image',size:100}]};
const notified=ui.messageEl({...message,result_notification:true});
assert.equal(notified.querySelectorAll('.result-card').length,0);
assert.equal(notified.querySelectorAll('.sys-detail').length,0);
assert.equal(notified.querySelectorAll('.attachment-image').length,1);
assert.ok(notified.textContent.includes('Work finished'));
assert.equal(ui.messageEl(message).querySelectorAll('.result-card').length,1);
console.log('Bot completion keeps the notification and photo without a result card; legacy cards remain visible.');
