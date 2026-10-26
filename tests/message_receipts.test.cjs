const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../covenant/static/app.js'), 'utf8');

function fixture() {
  const document = {activeElement: null};
  class Node {
    constructor(tag, cls='', text='') {this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.dataset={};this.open=false;}
    append(...nodes) {for (const node of nodes) {node.parent=this;this.children.push(node);}}
    replaceChildren() {this.children=[];}
    matches(sel) {return sel==='details[open]' ? this.tag==='details'&&this.open : sel.startsWith('.') ? this.className.split(' ').includes(sel.slice(1)) : this.tag===sel;}
    querySelectorAll(sel) {return this.children.flatMap(c=>[...(c.matches(sel)?[c]:[]),...c.querySelectorAll(sel)]);}
    querySelector(sel) {return this.querySelectorAll(sel)[0]||null;}
    contains(node) {return node===this||this.children.some(c=>c.contains(node));}
    closest(sel) {return this.matches(sel)?this:this.parent?.closest(sel);}
    focus() {document.activeElement=this;}
  }
  const ctx=vm.createContext({document, element:(...args)=>new Node(...args)});
  vm.runInContext(source.slice(source.indexOf('function updateMessageReceipts'),source.indexOf('async function refreshReceipts')),ctx);
  const article=new Node('article'), meta=new Node('div','message-meta'), stamp=new Node('time','message-time');
  meta.append(stamp);article.append(meta);
  return {ctx,article,meta,document,update:rows=>ctx.updateMessageReceipts(article,rows)};
}
const receipt={handle:'sol',status:'queued',queued_at:100,delivery_mode:null};

test('renders recipient status beside the timestamp and updates it without losing open details or focus',()=>{
  const f=fixture();f.update([receipt]);
  assert.equal(f.meta.children[1].className,'message-receipts');
  let details=f.article.querySelector('details');details.open=true;details.querySelector('summary').focus();
  f.update([{...receipt,status:'delivered',delivered_at:101,delivery_mode:'codex'}]);
  details=f.article.querySelector('details');
  assert.equal(details.querySelector('summary').textContent,'sol: Delivered');
  assert.equal(details.open,true);
  assert.equal(f.document.activeElement,details.querySelector('summary'));
  assert.equal(f.article.querySelectorAll('.message-receipts').length,1);
});

test('shows independent recipients and leaves old messages explicitly untracked',()=>{
  const f=fixture();f.update([receipt,{...receipt,handle:'opus',status:'answered',answered_at:102,reply_id:7}]);
  assert.deepEqual(f.article.querySelectorAll('summary').map(n=>n.textContent),['sol: Queued','opus: Answered']);
  f.update([]);
  assert.equal(f.article.querySelector('.receipt-unavailable').textContent,'Delivery tracking unavailable');
});

test('renders reported errors as text and preserves unchanged detail elements',()=>{
  const f=fixture(), row={...receipt,last_error:'<img src=x onerror=alert(1)>'};
  f.update([row]); const details=f.article.querySelector('details');
  assert.equal(f.article.querySelector('.receipt-error').textContent,row.last_error);
  assert.equal(f.article.querySelectorAll('img').length,0);
  f.update([row]);assert.equal(f.article.querySelector('details'),details);
});
