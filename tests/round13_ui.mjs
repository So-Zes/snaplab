import assert from 'node:assert/strict';
import {makeUI} from './ui_harness.mjs';
const {ui}=makeUI();
ui.S.sessions=[{id:'finished',status:'done',started_by:'b-builder'}];
assert.equal(ui.needsCount(),0,'Finished autonomous work must not ask for user attention');
ui.S.sessions.push({id:'review',status:'review',started_by:'user'});
assert.equal(ui.needsCount(),1);
console.log('Round 13 UI: finished Bot work does not ask for user attention.');
