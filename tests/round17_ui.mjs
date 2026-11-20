import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const window = {}, context = vm.createContext({window,Intl,document:{cookie:''},navigator:{language:'ru-RU'},localStorage:{getItem(){return null;},setItem(){}}});
for(const code of ['core','en','ja','ru']) vm.runInContext(fs.readFileSync(new URL('../web/i18n/'+code+'.js',import.meta.url),'utf8'),context);
const numbers=[0,1,2,5,11,21,22,1.5], words=['файлов','файл','файла','файлов','файлов','файл','файла','файла'];
for(const [i,n] of numbers.entries()) {
 assert.equal(window.snapT('ru','rc.files',{n}),`${n} ${words[i]}`);
 for(const key of ['ev.files.1','ev.files.n']) assert.ok(window.snapT('ru',key,{n:String(n),plus:1,minus:0}).includes(`${n} ${words[i]}`));
 assert.equal(window.snapT('en','rc.files',{n}),`${n} files`);
 assert.equal(window.snapT('ja','rc.files',{n}),window.SNAPLAB_I18N.ja['rc.files'].replace('{n}',n));
}
assert.equal(window.snapT('ru','projects.session1'),'1 задача SnapCode');
assert.equal(window.snapT('ru','bots.projects',{n:22}),'22 проекта');
assert.equal(window.snapT('ru','chats.questions.n',{n:21}),'21 вопрос ждёт ответа');
assert.equal(window.snapPickLang('ru-RU'),'ru');
assert.equal(window.snapBrowserLang(),'ru');
console.log('round17 UI: catalog, plural forms, legacy events and browser language passed');
