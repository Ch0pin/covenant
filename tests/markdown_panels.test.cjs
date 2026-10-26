const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const markdown = require('../covenant/static/markdown.js');

test('renders headings, emphasis, lists, tables, and code blocks', () => {
  const html = markdown.render('# Status\n\n**bold** and *italic* and ~~old~~\n\n- first\n- second\n\n| Key | Value |\n| --- | --- |\n| a | b |\n\n```js\nconst value = "<tag>";\n```');
  for (const tag of ['h1','strong','em','s','ul','li','table','th','td','pre','code']) assert.match(html,new RegExp('<'+tag+'(?:>| )'));
  assert.match(html,/&lt;tag&gt;/);
});

test('escapes arbitrary HTML and disables remote image loading', () => {
  for (const input of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<svg onload=alert(1)>', '<iframe src="https://example.com">']) {
    const html=markdown.render(input);
    assert.doesNotMatch(html,/<(?:script|img|svg|iframe)\b/i);
    assert.match(html,/&lt;/);
  }
  assert.doesNotMatch(markdown.render('![image](https://example.com/tracker.png)'),/<img\b/i);
});

test('only allows HTTP(S) and mailto links and isolates new tabs', () => {
  for (const url of ['javascript:alert%281%29','jav&#x61;script:alert%281%29','data:text/html,test','file:///etc/passwd','vbscript:msgbox%281%29','//evil.example','/api/logout']) {
    assert.doesNotMatch(markdown.render(`[click](${url})`),/<a\b/i,url);
  }
  const html=markdown.render('[safe](https://example.com) [email](mailto:person@example.com)');
  assert.match(html,/href="https:\/\/example.com"/);
  assert.match(html,/href="mailto:person@example.com"/);
  assert.equal((html.match(/rel="noopener noreferrer"/g)||[]).length,2);
  assert.equal((html.match(/target="_blank"/g)||[]).length,2);
});

const source=fs.readFileSync(path.join(__dirname,'../covenant/static/app.js'),'utf8');
function panelSetup(stored = new Map()) {
  const nodes=new Map();
  function $(selector) {
    if (!nodes.has(selector)) nodes.set(selector,{hidden:false,disabled:false,attrs:{},classes:new Set(),
      scrollHeight:100,scrollTop:0,clientHeight:100,
      focus(){this.focused=true;},setAttribute(k,v){this.attrs[k]=v;},classList:{toggle(name,on){if(on) nodes.get(selector).classes.add(name);else nodes.get(selector).classes.delete(name);}}});
    return nodes.get(selector);
  }
  const ctx=vm.createContext({$,state:{room:{id:'one'}},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)},closeRoomMenu(){},hideMentions(){},scrollBottom(){}});
  vm.runInContext(source.slice(source.indexOf('let panelPreferences'),source.indexOf('function showLogin')),ctx);
  return {$,ctx,stored};
}

test('collapses panels independently and restores preferences on reload', () => {
  let f=panelSetup(); f.$('#toggle-conversations').onclick();
  assert.equal(f.$('#conversations-panel').hidden,true);
  assert.equal(f.$('#participants-panel').hidden,false);
  assert.equal(f.$('#toggle-conversations').attrs['aria-expanded'],'false');
  f.$('#toggle-participants').onclick();
  f=panelSetup(f.stored);
  assert.equal(f.$('#conversations-panel').hidden,true);
  assert.equal(f.$('#participants-panel').hidden,true);
  f.$('#toggle-conversations').onclick();
  assert.equal(f.$('#conversations-panel').hidden,false);
  assert.equal(f.$('#toggle-conversations').attrs['aria-label'],'Hide chats');
  assert.equal(f.$('#toggle-conversations').title,'Hide chats');
  assert.equal(f.$('#participants-panel').hidden,true);
});

test('malformed preferences fall back to visible panels and empty chats disable participant toggle', () => {
  const f=panelSetup(new Map([['covenant-panels','{bad']]));
  assert.equal(f.$('#conversations-panel').hidden,false);
  f.ctx.state.room=null;f.ctx.applyPanelPreferences();
  assert.equal(f.$('#toggle-participants').disabled,true);
});

test('separates all leading recipients and retains Markdown indentation', () => {
  assert.deepEqual(markdown.splitRecipients('@dynamic_code_reviewer @operator FYI, **ready**'), {
    recipients:['dynamic_code_reviewer','operator'], body:'FYI, **ready**'
  });
  assert.deepEqual(markdown.splitRecipients('@operator @operator FYI'),{recipients:['operator'],body:'FYI'});
  const result=markdown.splitRecipients('@alice @operator\n    code here');
  assert.equal(result.body,'    code here');
  assert.match(markdown.render(result.body),/<pre><code>code here/);
  assert.deepEqual(markdown.splitRecipients('@alice Normal text mentioning @operator'),{
    recipients:['alice'],body:'Normal text mentioning @operator'
  });
  assert.deepEqual(markdown.splitRecipients('@alice@example.com'),{recipients:[],body:'@alice@example.com'});
});

test('objective and file-lock icons toggle independently and preserve labels across reload',()=>{
  const stored=new Map();let f=panelSetup(stored);
  f.ctx.state.room.objective='Review together';f.ctx.applyPanelPreferences();
  f.$('#toggle-objective').onclick();
  assert.equal(f.$('#objective-content').hidden,true);
  assert.equal(f.$('#toggle-objective').title,'Show room objective');
  assert.equal(f.$('#toggle-objective').attrs['aria-expanded'],'false');
  assert.equal(f.$('#file-lock-content').hidden,false);
  f.$('#toggle-file-locks').onclick();
  f=panelSetup(stored);
  assert.equal(f.$('#objective-content').hidden,true);
  assert.equal(f.$('#file-lock-content').hidden,true);
  assert.equal(f.$('#toggle-file-locks').attrs['aria-label'],'Show file locks');
  f.$('#toggle-file-locks').onclick();
  assert.equal(f.$('#file-lock-content').hidden,false);
  assert.equal(f.$('#toggle-file-locks').title,'Hide file locks');
});

test('side panel headers hand off to edge reopen icons and restore keyboard focus',()=>{
  const f=panelSetup();
  assert.equal(f.$('#reopen-conversations').hidden,true);
  assert.equal(f.$('#participants-reopen-rail').hidden,true);
  f.$('#toggle-conversations').onclick();
  assert.equal(f.$('#reopen-conversations').hidden,false);
  assert.equal(f.$('#reopen-conversations').focused,true);
  f.$('#reopen-conversations').onclick();
  assert.equal(f.$('#conversations-panel').hidden,false);
  assert.equal(f.$('#reopen-conversations').hidden,true);
  assert.equal(f.$('#toggle-conversations').focused,true);
  f.$('#toggle-participants').onclick();
  assert.equal(f.$('#participants-reopen-rail').hidden,false);
  assert.equal(f.$('#reopen-participants').focused,true);
  f.$('#reopen-participants').onclick();
  assert.equal(f.$('#participants-panel').hidden,false);
  assert.equal(f.$('#participants-reopen-rail').hidden,true);
});
