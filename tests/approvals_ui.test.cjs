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
  showModal(){this.open=true;}close(){this.open=false;this.onclose?.();}focus(){document.activeElement=this;}
 }
 const nodes=new Map(),listeners={};let refresh;
 const state={me:{username:'operator'},room:{id:'a',status:'active'}},network={rows:[],connections:[],fail:false,decisions:[],policies:{},policyChanges:[]};
 const ctx=vm.createContext({state,document,$:s=>{if(!nodes.has(s))nodes.set(s,new Node('div'));return nodes.get(s);},element:(...args)=>new Node(...args),timeText:()=>'',notificationsEnabled:false,toast(){},window:{addEventListener:(name,fn)=>listeners[name]=fn},setInterval:fn=>refresh=fn,
 api:async(path,options)=>{if(network.fail)throw Object.assign(new Error('offline'),{status:503});if(options&&path.endsWith('/approval-policy')){const value=JSON.parse(options.body).auto_approve;const room=path.split('/')[3];network.policyChanges.push({room,value});network.policies[room]=value;return {auto_approve:value,approved_count:0};}if(options){network.decisions.push(JSON.parse(options.body).decision);return {status:'decided'};}return {requests:network.rows,connections:network.connections,auto_approve_rooms:network.policies};}});
 vm.runInContext(source,ctx);return{nodes,network,state,refresh,listeners,Node,ctx};
}
const request={id:'one',status:'pending',handle:'sol',harness:'codex',room_title:'Room',session_id:'session',kind:'command',can_approve:true,details:{command:'<script>untrusted</script>'}};
function openRequest(f,id='one'){
 const row=f.nodes.get('#pending-approvals').querySelectorAll('.approval-row').find(n=>n.dataset.approvalId===id);
 assert.ok(row);row.onclick();
 return f.nodes.get('#approval-request-body').children[0];
}
function action(f,id,decision){
 openRequest(f,id);
 return f.nodes.get('#approval-request-actions').querySelectorAll('button').find(b=>b.dataset.approvalAction===id+':'+decision);
}
test('pending rows open a dialog and disappear after either decision even during stale refresh',async()=>{
 for(const decision of ['approve','reject']){
  const f=fixture();f.network.rows=[request];await f.refresh();
  const pending=f.nodes.get('#pending-approvals'),row=pending.children[0];
  assert.equal(row.tag,'button');assert.equal(row.getAttribute('aria-haspopup'),'dialog');
  assert.equal(pending.querySelectorAll('pre').length,0);
  const card=openRequest(f);
  assert.equal(f.nodes.get('#approval-request-dialog').open,true);
  assert.equal(card.tag,'article');assert.equal(card.querySelectorAll('script').length,0);
  f.network.rows=[request,{...request,id:'two'}];await f.refresh();
  assert.equal(f.nodes.get('#approval-request-body').children[0],card);
  assert.equal(card.dataset.approvalId,'one');
  await action(f,'one',decision).onclick();
  assert.deepEqual(f.network.decisions,[decision]);
  assert.equal(f.nodes.get('#approval-request-dialog').open,false);
  assert.equal(pending.querySelectorAll('.approval-row').length,1);
  assert.equal(f.nodes.get('#approvals-list').querySelectorAll('.approval-card').length,1);
  await f.refresh();assert.equal(pending.querySelectorAll('.approval-row').length,1);
 }
});
test('outage disables approval actions and logout immediately clears request details',async()=>{
 const f=fixture();f.network.rows=[request];await f.refresh();openRequest(f);f.network.fail=true;await f.refresh();
 assert.ok(f.nodes.get('#pending-approvals').querySelectorAll('button').every(b=>b.disabled));
 assert.ok(f.nodes.get('#approval-request-actions').querySelectorAll('button').every(b=>b.disabled));
 assert.equal(f.nodes.get('#approval-request-error').hidden,false);
 f.state.me=null;await f.listeners['covenant-login-change']();
 assert.equal(f.nodes.get('#pending-approvals').children.length,0);
 assert.equal(f.nodes.get('#approvals-list').children.length,0);
 assert.equal(f.nodes.get('#approval-request-dialog').open,false);
 assert.equal(f.nodes.get('#approval-request-body').children.length,0);
});

test('Codex MCP permission cards show the native question and arguments and resolve either decision',async()=>{
 for(const decision of ['approve','reject']){
  const f=fixture();
  const details={tool_name:'fixture · MCP tool',reason:'Allow the fixture MCP server to run tool "approval_probe"?',tool_input:{text:'hello'}};
  f.network.rows=[{...request,kind:'tool',details}];await f.refresh();
  const pending=f.nodes.get('#pending-approvals'),card=openRequest(f);
  assert.equal(f.nodes.get('#approval-request-dialog').open,true);
  assert.ok(card.querySelectorAll('p').some(node=>node.textContent===details.reason));
  assert.ok(card.querySelectorAll('pre').some(node=>JSON.parse(node.textContent).text==='hello'));
  const button=action(f,'one',decision);
  assert.ok(!button.disabled);await button.onclick();
  assert.deepEqual(f.network.decisions,[decision]);
  assert.equal(pending.querySelectorAll('.approval-row').length,0);
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
  const button=action(f,id,decision);
  await button.onclick();
  for(const selector of indicators)assert.equal(f.nodes.get(selector).dataset.approvalPending,id==='one'?'true':'false');
 }
 f.network.rows=[{...request,id:'three'}];await f.refresh();
 f.network.fail=true;await f.refresh();
 assert.equal(f.nodes.get('#reopen-participants').dataset.approvalPending,'true');
 f.state.me=null;await f.listeners['covenant-login-change']();
 for(const selector of indicators)assert.equal(f.nodes.get(selector).dataset.approvalPending,'false');
});

 test('participant forwarding status updates after connection loss and clears on logout',async()=>{
  const f=fixture();
  await f.refresh();
  const node=new f.Node('small','approval-connection');node.dataset.agentId='agent';
  f.nodes.get('#agent-list').append(node);
  f.network.connections=[{agent_id:'agent',harness:'codex',state:'connected'}];
  await f.refresh();assert.equal(node.textContent,'Approvals: connected');
  f.network.fail=true;await f.refresh();assert.equal(node.textContent,'Approvals: disconnected');
  f.network.fail=false;f.network.connections=[{agent_id:'agent',harness:'claude',state:'ready'}];
  await f.refresh();assert.equal(node.textContent,'Approvals: ready');
  f.state.me=null;await f.refresh();assert.equal(node.textContent,'Approvals: setup required');
 });


test('chat dots track each room, survive rerenders and outages, and clear after decisions or logout',async()=>{
 const f=fixture();await f.refresh();
 function chat(id){
  const button=new f.Node('button','room-item');button.dataset.roomId=id;
  const dot=new f.Node('span','room-approval-dot');dot.hidden=true;button.append(dot);
  f.nodes.get('#room-list').append(button);f.ctx.CovenantRoomApprovals.paint(button);
  return {button,dot};
 }
 const a=chat('a'),b=chat('b'),c=chat('c');
 f.network.rows=[{...request,room_id:'a'},{...request,id:'two',room_id:'a'},{...request,id:'three',room_id:'b'}];
 await f.refresh();
 assert.equal(a.dot.hidden,false);assert.equal(b.dot.hidden,false);assert.equal(c.dot.hidden,true);
 assert.equal(a.dot.title,'2 pending approvals need your decision');
 assert.equal(a.button.getAttribute('aria-description'),a.dot.title);
 // Room selection and list refresh recreate buttons independently of approval polling.
 const recreated=chat('a');assert.equal(recreated.dot.hidden,false);
 f.network.fail=true;await f.refresh();assert.equal(a.dot.hidden,false);
 f.network.fail=false;await f.refresh();
 for(const [id,decision] of [['one','approve'],['two','reject']]){
  const control=action(f,id,decision);
  await control.onclick();
  assert.equal(a.dot.hidden,id==='two');assert.equal(recreated.dot.hidden,id==='two');
  assert.equal(b.dot.hidden,false);
 }
 // Server-side resolution/expiry also clears the relevant room.
 f.network.rows=[{...request,id:'three',room_id:'b',status:'expired'}];await f.refresh();
 assert.equal(b.dot.hidden,true);
 f.network.rows=[{...request,id:'four',room_id:'b'}];await f.refresh();assert.equal(b.dot.hidden,false);
 f.state.me=null;await f.refresh();assert.equal(b.dot.hidden,true);
});

test('expired approvals remain visibly discoverable outside the collapsed list',async()=>{
 const f=fixture();f.network.rows=[{...request,status:'expired',native_resolved:0}];await f.refresh();
 const notice=f.nodes.get('#expired-approvals-notice');
 assert.equal(notice.hidden,false);assert.match(notice.textContent,/1 expired/);
 await f.nodes.get('#toggle-approvals').onclick();assert.equal(notice.hidden,false);
 await notice.onclick();assert.equal(f.nodes.get('#approvals-dialog').open,true);
 assert.ok(f.nodes.get('#approvals-list').querySelectorAll('p').some(n=>n.textContent.includes('unconfirmed')));
 f.network.rows=[{...request,status:'expired',native_resolved:1,dismissed_at:123}];await f.refresh();
 assert.equal(notice.hidden,true);
 assert.ok(f.nodes.get('#approvals-list').querySelectorAll('p').some(n=>n.textContent.includes('confirmed this request')));
});


test('open dialog reflects expiry and external resolution without leaving decision buttons enabled',async()=>{
 for(const status of ['expired','resolved','cancelled']){
  const f=fixture();f.network.rows=[request];await f.refresh();openRequest(f);
  f.network.rows=[{...request,status}];await f.refresh();
  assert.equal(f.nodes.get('#approval-request-dialog').open,true);
  assert.equal(f.nodes.get('#approval-request-actions').querySelectorAll('button').length,0);
  assert.equal(f.nodes.get('#pending-approvals').querySelectorAll('.approval-row').length,0);
 }
});
test('close preserves the pending request and returns focus to its row',async()=>{
 const f=fixture();f.network.rows=[request];await f.refresh();openRequest(f);
 f.nodes.get('#approval-request-dialog').close();
 assert.equal(f.ctx.document.activeElement.dataset.approvalId,'one');
 assert.equal(f.nodes.get('#pending-approvals').querySelectorAll('.approval-row').length,1);
 assert.equal(f.network.decisions.length,0);
});


test('auto-approve reflects and changes only the selected room policy',async()=>{
 const f=fixture();await f.refresh();
 const checkbox=f.nodes.get('#auto-approve-room');
 assert.equal(checkbox.checked,false);assert.equal(checkbox.disabled,false);
 checkbox.checked=true;await checkbox.onchange();
 assert.deepEqual(f.network.policyChanges,[{room:'a',value:true}]);
 assert.equal(checkbox.checked,true);
 f.state.room={id:'b',status:'active'};f.ctx.CovenantApprovals.renderPolicy();
 assert.equal(checkbox.checked,false);
 f.state.room={id:'a',status:'paused'};f.ctx.CovenantApprovals.renderPolicy();
 assert.equal(checkbox.checked,true);assert.equal(checkbox.disabled,false);
 checkbox.checked=false;await checkbox.onchange();assert.equal(checkbox.checked,false);
 assert.equal(checkbox.disabled,true);
});
test('auto-approve is disabled when unavailable and clears on logout',async()=>{
 const f=fixture();f.network.policies={a:true};await f.refresh();
 const checkbox=f.nodes.get('#auto-approve-room');assert.equal(checkbox.checked,true);
 f.network.fail=true;await f.refresh();assert.equal(checkbox.disabled,true);
 assert.match(f.nodes.get('#auto-approve-help').textContent,/unavailable/);
 f.network.fail=false;f.network.policies=undefined;await f.refresh();assert.equal(checkbox.disabled,true);
 f.state.me=null;await f.refresh();assert.equal(checkbox.checked,false);assert.equal(checkbox.disabled,true);
});
test('automatically approved decisions are identified in history',async()=>{
 const f=fixture();f.network.rows=[{...request,status:'decided',decision:'approve',auto_approved:true}];await f.refresh();
 assert.ok(f.nodes.get('#approvals-list').querySelectorAll('p').some(n=>n.textContent.includes('Auto-approved')));
});
