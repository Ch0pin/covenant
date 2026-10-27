const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/approvals.js'),'utf8');
function fixture(){
 const document={activeElement:null};
 class Node {
  constructor(tag,cls='',text=''){this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.dataset={};this.open=false;this.hidden=false;this.attrs={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];}
  querySelectorAll(sel){return this.children.flatMap(c=>[...((sel.startsWith('.')?c.className.split(' ').includes(sel.slice(1)):c.tag===sel)?[c]:[]),...c.querySelectorAll(sel)]);}
  setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k];}
  showModal(){this.open=true;}close(){this.open=false;}focus(){document.activeElement=this;}
 }
 const nodes=new Map(),listeners={};let refresh;
 const state={me:{username:'operator'}},network={rows:[],fail:false,decisions:[]};
 const ctx=vm.createContext({state,document,$:s=>{if(!nodes.has(s))nodes.set(s,new Node('div'));return nodes.get(s);},element:(...args)=>new Node(...args),timeText:()=>'',notificationsEnabled:false,toast(){},window:{addEventListener:(name,fn)=>listeners[name]=fn},setInterval:fn=>refresh=fn,
 api:async(path,options)=>{if(network.fail)throw Object.assign(new Error('offline'),{status:503});if(options){network.decisions.push(JSON.parse(options.body).decision);return {status:'decided'};}return {requests:network.rows};}});
 vm.runInContext(source,ctx);return{nodes,network,state,refresh,listeners};
}
const request={id:'one',status:'pending',handle:'sol',harness:'codex',room_title:'Room',session_id:'session',kind:'command',can_approve:true,details:{command:'<script>untrusted</script>'}};
test('cards start collapsed, preserve expansion, and disappear after either decision even during stale refresh',async()=>{
 for(const decision of ['approve','reject']){
  const f=fixture();f.network.rows=[request];await f.refresh();
  const pending=f.nodes.get('#pending-approvals'),card=pending.children[0];
  assert.equal(card.tag,'details');assert.equal(card.open,false);
  card.open=true;f.network.rows=[request,{...request,id:'two'}];await f.refresh();
  assert.equal(pending.children[0].open,true);
  assert.equal(pending.querySelectorAll('script').length,0);
  const control=pending.children[0].querySelectorAll('button').find(b=>b.dataset.approvalAction==='one:'+decision);
  await control.onclick();
  assert.deepEqual(f.network.decisions,[decision]);
  assert.equal(pending.querySelectorAll('.approval-card').length,1);
  assert.equal(f.nodes.get('#approvals-list').querySelectorAll('.approval-card').length,1);
  await f.refresh();assert.equal(pending.querySelectorAll('.approval-card').length,1);
 }
});
test('outage disables approval actions and logout immediately clears request details',async()=>{
 const f=fixture();f.network.rows=[request];await f.refresh();f.network.fail=true;await f.refresh();
 assert.ok(f.nodes.get('#pending-approvals').querySelectorAll('button').every(b=>b.disabled));
 f.state.me=null;await f.listeners['covenant-login-change']();
 assert.equal(f.nodes.get('#pending-approvals').children.length,0);
 assert.equal(f.nodes.get('#approvals-list').children.length,0);
});

test('Codex MCP permission cards show the native question and arguments and resolve either decision',async()=>{
 for(const decision of ['approve','reject']){
  const f=fixture();
  const details={tool_name:'fixture · MCP tool',reason:'Allow the fixture MCP server to run tool "approval_probe"?',tool_input:{text:'hello'}};
  f.network.rows=[{...request,kind:'tool',details}];await f.refresh();
  const pending=f.nodes.get('#pending-approvals'),card=pending.children[0];
  assert.equal(card.open,false);
  assert.ok(card.querySelectorAll('p').some(node=>node.textContent===details.reason));
  assert.ok(card.querySelectorAll('pre').some(node=>JSON.parse(node.textContent).text==='hello'));
  const button=card.querySelectorAll('button').find(b=>b.dataset.approvalAction==='one:'+decision);
  assert.ok(!button.disabled);await button.onclick();
  assert.deepEqual(f.network.decisions,[decision]);
  assert.equal(pending.querySelectorAll('.approval-card').length,0);
 }
});

test('pending dots survive collapsed panels and clear only after all decisions or logout',async()=>{
 const f=fixture();f.network.rows=[request,{...request,id:'two'}];await f.refresh();
 const indicators=['#reopen-participants','#toggle-participants','#toggle-approvals','#mobile-details'];
 for(const selector of indicators){
  const node=f.nodes.get(selector);
  assert.equal(node.dataset.approvalPending,'true');
  assert.equal(node.getAttribute('aria-description'),'2 pending approvals need your decision');
 }
 // Collapsing the approvals section keeps the indicator on its visible toggle.
 await f.nodes.get('#toggle-approvals').onclick();
 assert.equal(f.nodes.get('#pending-approvals').hidden,true);
 assert.equal(f.nodes.get('#toggle-approvals').dataset.approvalPending,'true');
 for(const [id,decision] of [['one','approve'],['two','reject']]){
  const button=f.nodes.get('#pending-approvals').querySelectorAll('button').find(b=>b.dataset.approvalAction===id+':'+decision);
  await button.onclick();
  for(const selector of indicators)assert.equal(f.nodes.get(selector).dataset.approvalPending,id==='one'?'true':'false');
 }
 f.network.rows=[{...request,id:'three'}];await f.refresh();
 f.network.fail=true;await f.refresh();
 assert.equal(f.nodes.get('#reopen-participants').dataset.approvalPending,'true');
 f.state.me=null;await f.listeners['covenant-login-change']();
 for(const selector of indicators)assert.equal(f.nodes.get(selector).dataset.approvalPending,'false');
});
