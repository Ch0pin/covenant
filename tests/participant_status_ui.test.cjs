const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../covenant/static/participant-status.js'),'utf8');
const app=fs.readFileSync(path.join(__dirname,'../covenant/static/app.js'),'utf8');
const renderer=app.slice(app.indexOf('function renderAgents()'),app.indexOf('async function roomAction'));
function fixture(){
 class Node {
  constructor(tag,cls='',text=''){this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.dataset={};this.attrs={};}
  append(...nodes){for(const n of nodes){n.parentElement=this;this.children.push(n);}}
  replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attrs[k]=v;}
  querySelectorAll(sel){return this.children.flatMap(c=>[...((sel.startsWith('.')?c.className.split(' ').includes(sel.slice(1)):c.tag===sel)?[c]:[]),...c.querySelectorAll(sel)]);}
  querySelector(sel){return this.querySelectorAll(sel)[0];}
 }
 const nodes=new Map(),listeners={};let tick,now=1000000;
 const state={me:{username:'operator'},room:{id:'room-a',status:'active'},agents:[{id:'sol-id',handle:'sol',revoked:false}]};
 const network={data:{server_time:1000,statuses:{}},fail:false,handler:null};
 class Clock extends Date{static now(){return now;}}
 const $=selector=>{if(!nodes.has(selector))nodes.set(selector,new Node('div'));return nodes.get(selector);};
 const document={querySelectorAll:sel=>[...nodes.values()].flatMap(n=>n.querySelectorAll(sel))};
 const context=vm.createContext({state,document,$,Date:Clock,setInterval:fn=>tick=fn,window:{addEventListener:(n,f)=>listeners[n]=f},
  element:(...args)=>new Node(...args),senderAvatar:()=>new Node('div','avatar'),seenText:()=> 'Seen just now',renewAgent(){},revokeAgent(){},
  api:async url=>{if(network.handler)return network.handler(url);if(network.fail)throw new Error('offline');return network.data;}});
 vm.runInContext(source+'\n'+renderer,context);context.renderAgents();
 return{context,nodes,state,network,listeners,tick,setNow:value=>now=value};
}
const status={code:'approval',label:'Waiting for approval',level:'warning',since:820,detail:'Native action awaiting a decision.',telemetry:'Native state available'};
test('circle is beside the name, status precedes Seen and approval connection, controls remain',async()=>{
 const f=fixture();f.network.data.statuses={'sol-id':status};await f.tick();
 const info=f.nodes.get('#agent-list').querySelector('.participant-info');
 assert.equal(info.children[0].className,'participant-name');
 assert.equal(info.children[0].children[0].textContent,'@sol');
 assert.equal(info.children[0].children[1].dataset.level,'warning');
 assert.equal(info.children[1].textContent,'Waiting for approval · 3 min');
 assert.equal(info.children[2].textContent,'Seen just now');
 assert.equal(info.children[3].className,'approval-connection');
 assert.deepEqual(f.nodes.get('#agent-list').querySelectorAll('button').map(n=>n.textContent),['Renew token','Revoke']);
 assert.equal(info.querySelector('.participant-status-dot').attrs['aria-hidden'],'true');
});
test('status updates replace text and colour without replacing controls and network loss clears stale green',async()=>{
 const f=fixture();const button=f.nodes.get('#agent-list').querySelector('button');
 f.network.data.statuses={'sol-id':{...status,label:'Idle',level:'ok'}};await f.tick();
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status-dot').dataset.level,'ok');
 f.network.data.statuses={'sol-id':{...status,label:'Cybersecurity policy restriction',level:'error'}};await f.tick();
 assert.match(f.nodes.get('#agent-list').querySelector('.participant-status').textContent,/Cybersecurity/);
 assert.equal(f.nodes.get('#agent-list').querySelector('button'),button);
 f.network.fail=true;await f.tick();
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status-dot').dataset.level,'neutral');
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status').textContent,'Status unavailable');
});
test('room switch and logout discard in-flight status responses',async()=>{
 const f=fixture();let resolve;
 f.network.handler=()=>new Promise(r=>resolve=r);
 const pending=f.tick();f.state.room={id:'room-b',status:'active'};
 resolve({server_time:1000,statuses:{'sol-id':status}});await pending;
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status').textContent,'Status unavailable');
 f.network.handler=null;f.state.me=null;await f.listeners['covenant-login-change']();
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status-dot').dataset.level,'neutral');
});
test('rerendered participants retain fresh status and older samples become unknown',async()=>{
 const f=fixture();f.network.data.statuses={'sol-id':status};await f.tick();f.context.renderAgents();
 assert.match(f.nodes.get('#agent-list').querySelector('.participant-status').textContent,/Waiting for approval/);
 f.setNow(1011000);f.context.renderAgents();
 assert.equal(f.nodes.get('#agent-list').querySelector('.participant-status').textContent,'Status unavailable');
});
