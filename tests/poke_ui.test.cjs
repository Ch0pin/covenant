const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../covenant/static/poke.js'),'utf8');
const app=fs.readFileSync(path.join(__dirname,'../covenant/static/app.js'),'utf8');
const renderer=app.slice(app.indexOf('function renderAgents()'),app.indexOf('async function roomAction'));
function fixture(){
 class Node {
  constructor(tag,cls='',text=''){this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.dataset={};this.attrs={};this.hidden=false;this.disabled=false;}
  append(...nodes){for(const n of nodes){n.parentElement=this;this.children.push(n);}}
  replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attrs[k]=v;}
  matches(sel){const m=sel.match(/^\.([\w-]+)(?:\[data-agent-id="([^"]+)"\])?$/);return m?this.className.split(' ').includes(m[1])&&(!m[2]||this.dataset.agentId===m[2]):this.tag===sel;}
  querySelectorAll(sel){return this.children.flatMap(c=>[...(c.matches(sel)?[c]:[]),...c.querySelectorAll(sel)]);}
  querySelector(sel){return this.querySelectorAll(sel)[0];}
 }
 const nodes=new Map(),listeners={},toasts=[],posts=[];let tick,now=1000000;
 const state={me:{username:'operator'},room:{id:'room-a',status:'active'},agents:[{id:'sol-id',handle:'sol',revoked:false},{id:'old-id',handle:'old',revoked:true}]};
 const receipts={};
 class Clock extends Date{static now(){return now;}}
 const $=selector=>{if(!nodes.has(selector))nodes.set(selector,new Node('div'));return nodes.get(selector);};
 const document={querySelectorAll:sel=>[...nodes.values()].flatMap(n=>n.querySelectorAll(sel)),querySelector:sel=>document.querySelectorAll(sel)[0]};
 const context=vm.createContext({state,document,$,Date:Clock,setInterval:fn=>tick=fn,window:{addEventListener:(n,f)=>listeners[n]=f},
  element:(...args)=>new Node(...args),senderAvatar:()=>new Node('div','avatar'),seenText:()=>'Seen just now',renewAgent(){},revokeAgent(){},
  toast:m=>toasts.push(m),crypto:{randomUUID:()=>'uuid-1'},
  api:async(url,options)=>{
   if(options?.method==='POST'){posts.push({url,body:JSON.parse(options.body)});return {id:77};}
   return {receipts};
  }});
 vm.runInContext(source+'\n'+renderer,context);context.renderAgents();
 const list=nodes.get('#agent-list');
 return{context,state,list,toasts,posts,receipts,listeners,tick:()=>tick(),advance:s=>now+=s*1000,
  line:()=>list.querySelector('.poke-result[data-agent-id="sol-id"]'),button:()=>list.querySelector('.poke-button[data-agent-id="sol-id"]')};
}
test('poke button precedes renew token for active agents only and sends a tracked message',async()=>{
 const f=fixture();
 assert.deepEqual(f.list.querySelectorAll('button').map(n=>n.textContent),['Poke','Renew token','Revoke','Renew token']);
 assert.equal(f.line().hidden,true);
 await f.button().onclick();
 assert.deepEqual(f.posts,[{url:'/api/rooms/room-a/messages',body:{text:'@sol Poke from the operator: are you still there? Acknowledge or reply briefly.',client_message_id:'uuid-1'}}]);
 assert.deepEqual(f.toasts,['Poked @sol']);
 assert.equal(f.line().hidden,false);
 assert.equal(f.line().textContent,'Poked · waiting 0 s');
 assert.equal(f.button().disabled,true);
 await f.button().onclick();
 assert.equal(f.posts.length,1,'a waiting poke is not sent twice');
});
test('the result shows how long the agent took to acknowledge or answer',async()=>{
 const f=fixture();await f.button().onclick();
 f.advance(5);await f.tick();
 assert.equal(f.line().textContent,'Poked · waiting 5 s');
 f.receipts[77]=[{handle:'sol',queued_at:1000,delivered_at:1002,acknowledged_at:1008,answered_at:null}];
 await f.tick();
 assert.equal(f.line().textContent,'Poke acknowledged in 8 s');
 assert.equal(f.line().dataset.level,'ok');
 assert.equal(f.button().disabled,false);
 f.receipts[77]=[{handle:'sol',queued_at:1000,delivered_at:1002,acknowledged_at:1008,answered_at:1021}];
 await f.tick();
 assert.equal(f.line().textContent,'Poke answered in 21 s','a reply after the acknowledgement upgrades the result');
 f.context.renderAgents();
 assert.equal(f.line().textContent,'Poke answered in 21 s','re-rendering the list keeps the result');
});
test('silence for a minute is reported as a warning, the button comes back, and a late answer still shows',async()=>{
 const f=fixture();await f.button().onclick();
 f.advance(59);await f.tick();
 assert.equal(f.button().disabled,true);
 f.advance(1);await f.tick();
 assert.equal(f.line().textContent,'No response to poke after 1 min');
 assert.equal(f.line().dataset.level,'warning');
 assert.equal(f.button().disabled,false);
 f.advance(30);await f.tick();
 assert.equal(f.line().textContent,'No response to poke after 1 min 30 s','the warning keeps counting while it watches');
 f.receipts[77]=[{handle:'sol',queued_at:1000,delivered_at:1002,acknowledged_at:null,answered_at:1090}];
 await f.tick();
 assert.equal(f.line().textContent,'Poke answered in 1 min 30 s');
 assert.equal(f.line().dataset.level,'ok');
});
test('watching stops half an hour after the poke',async()=>{
 const f=fixture();await f.button().onclick();
 f.advance(30*60);await f.tick();
 assert.equal(f.line().textContent,'No response to poke after 30 min');
 f.receipts[77]=[{handle:'sol',queued_at:1000,delivered_at:1002,acknowledged_at:2900,answered_at:null}];
 f.advance(600);await f.tick();
 assert.equal(f.line().textContent,'No response to poke after 30 min','receipts are no longer fetched after the window');
});
test('results belong to one chat and disappear on logout',async()=>{
 const f=fixture();await f.button().onclick();
 f.state.room={id:'room-b',status:'active'};f.context.renderAgents();
 assert.equal(f.line().hidden,true);assert.equal(f.button().disabled,false);
 f.state.room={id:'room-a',status:'active'};f.context.renderAgents();
 assert.equal(f.line().hidden,false);
 f.state.me=null;await f.listeners['covenant-login-change']();
 f.state.me={username:'operator'};f.context.renderAgents();
 assert.equal(f.line().hidden,true);
});
