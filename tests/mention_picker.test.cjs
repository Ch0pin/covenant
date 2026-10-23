const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../covenant/static/app.js'), 'utf8');

function setup() {
  const nodes = new Map();
  function node() {
    return {value: '', selectionStart: 0, selectionEnd: 0, maxLength: 16000, children: [], attrs: {}, listeners: {},
      hidden: true, disabled: false, setAttribute(k,v) {this.attrs[k]=v;}, removeAttribute(k) {delete this.attrs[k];},
      append(...children) {this.children.push(...children);}, replaceChildren() {this.children=[];},
      addEventListener(k,fn) {this.listeners[k]=fn;}, scrollIntoView() {}, focus() {},
      setSelectionRange(start,end) {this.selectionStart=start;this.selectionEnd=end;}};
  }
  const $ = selector => {if (!nodes.has(selector)) nodes.set(selector,node());return nodes.get(selector);};
  const input = $('#message-text');
  const ctx = vm.createContext({$, document: {activeElement: input}, state: {room:{id:'room'},agents:[
    {handle:'dynamic_code_reviewer'}, {handle:'static_code_reviewer'}, {handle:'disabled_agent',revoked:1}
  ]}, element: (tag,cls,text) => Object.assign(node(),{textContent:text}), senderAvatar: node});
  const start = source.indexOf('const mentionState =');
  vm.runInContext(source.slice(start,source.indexOf('$("#message-form").addEventListener("submit"',start)),ctx);
  const keyStart = source.indexOf('$("#message-text").addEventListener("keydown"',start);
  vm.runInContext(source.slice(keyStart,source.indexOf('async function refreshCurrent()',keyStart)),ctx);
  let sends=0; $('#message-form').requestSubmit=()=>sends++;
  function type(text,caret=text.length) {input.value=text;input.setSelectionRange(caret,caret);input.listeners.input();}
  function key(key,options={}) {const e={key,...options,preventDefault(){this.prevented=true;}};input.listeners.keydown(e);return e;}
  return {$,ctx,input,type,key,sends:()=>sends};
}

test('filters recipients, excludes revoked agents, and Tab completes without sending',()=>{
  const f=setup(); f.type('@');
  assert.equal(f.$('#mention-options').children.length,4);
  f.type('@DYN'); assert.equal(f.$('#mention-options').children.length,1);
  assert.equal(f.key('Tab').prevented,true);
  assert.equal(f.input.value,'@dynamic_code_reviewer ');
  assert.equal(f.input.selectionStart,f.input.value.length);
  assert.equal(f.$('#mention-picker').hidden,true); assert.equal(f.sends(),0);
});

test('completion preserves existing text and replaces the entire token at the caret',()=>{
  const f=setup(); f.type('@dyn_old Please review',4); f.key('Tab');
  assert.equal(f.input.value,'@dynamic_code_reviewer Please review');
  assert.equal(f.input.selectionStart,'@dynamic_code_reviewer '.length);
  f.type('Hello @sta'); f.key('Enter');
  assert.equal(f.input.value,'Hello @static_code_reviewer '); assert.equal(f.sends(),0);
});

test('arrow keys choose a recipient, Escape dismisses, and normal Tab is preserved',()=>{
  const f=setup(); f.type('@'); f.key('ArrowDown'); f.key('Tab');
  assert.equal(f.input.value,'@static_code_reviewer ');
  f.type('@'); f.key('Escape'); assert.equal(f.$('#mention-picker').hidden,true);
  assert.equal(f.key('Tab').prevented,undefined);
  f.type('@dy'); assert.equal(f.key('Tab',{shiftKey:true}).prevented,undefined);
  assert.equal(f.key('Enter',{isComposing:true}).prevented,undefined);
  assert.equal(f.sends(),0);
});

test('ignores email addresses, absent matches, text selections, and archived composers',()=>{
  const f=setup();
  for (const text of ['person@dynamic','@nobody','@disabled']) {f.type(text);assert.equal(f.$('#mention-picker').hidden,true);}
  f.type('@dy'); f.input.selectionEnd=3;f.input.selectionStart=1;f.ctx.updateMentions();
  assert.equal(f.$('#mention-picker').hidden,true);
  f.input.disabled=true;f.type('@');assert.equal(f.$('#mention-picker').hidden,true);
});

test('click completion works and respects the message length limit',()=>{
  const f=setup();f.type('@eve');f.$('#mention-options').children[0].onclick();
  assert.equal(f.input.value,'@everyone ');
  f.input.maxLength=5;f.type('@dyn');f.key('Tab');assert.equal(f.input.value,'@dyn');
  assert.match(f.$('#message-error').textContent,/too long/);
});
