const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/participant-menu.js'),'utf8');
function fixture(){
 let document;
 class Node {
  constructor(tag,cls='',text=''){Object.assign(this,{tag,className:cls,textContent:text,children:[],dataset:{},style:{},attrs:{},hidden:true,offsetWidth:190,offsetHeight:200});}
  append(n){this.children.push(n);}
  replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attrs[k]=v;}
  querySelectorAll(){return this.children.filter(n=>!n.disabled);}
  querySelector(){return this.querySelectorAll()[0];}
  contains(n){return n===this||this.children.includes(n);}
  focus(){document.activeElement=this;}
  getBoundingClientRect(){return {right:350,bottom:500};}
 }
 const menu=new Node('div'),events={},windowEvents={},calls=[];
 const state={me:{},room:{id:'room',status:'active'},agents:[{id:'a',handle:'sol',revoked:false}]};
 let row,controls,ctx;
 document={activeElement:null,querySelector:s=>s==='#participant-menu'?menu:row?.children.find(n=>n.dataset.agentId==='a'),addEventListener:(n,f)=>events[n]=f};
 function renderAgents(){
  ctx.CovenantParticipantMenu.sync();row=new Node('div');controls=new Node('div');
  for(const name of ['Poke','Rename','Renew token','Revoke','Remove']){
   const b=new Node('button','',name);b.onclick=()=>calls.push(name);controls.append(b);
  }
  ctx.CovenantParticipantMenu.attach(row,controls,state.agents[0]);
 }
 ctx=vm.createContext({state,document,element:(...a)=>new Node(...a),renderAgents,window:{innerWidth:375,innerHeight:600,addEventListener:(n,f)=>windowEvents[n]=f}});
 vm.runInContext(source,ctx);renderAgents();
 return {ctx,state,menu,events,windowEvents,calls,renderAgents,row:()=>row,trigger:()=>row.children[0]};
}
test('right click opens bounded menu, refresh preserves it, action runs once and closes',()=>{
 const f=fixture();let prevented=false;
 f.row().oncontextmenu({clientX:370,clientY:590,preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.equal(f.menu.hidden,false);
 assert.equal(f.row().dataset.menuOpen,'true');
 assert.equal(f.trigger().attrs['aria-expanded'],'true');
 assert.equal(f.menu.style.left,'177px');assert.equal(f.menu.style.top,'392px');
 assert.deepEqual(f.menu.children.map(n=>n.textContent),['Poke','Rename','Renew token','Revoke','Remove']);
 f.renderAgents();assert.equal(f.menu.hidden,false);
 assert.equal(f.row().dataset.menuOpen,'true');
 f.menu.children[1].onclick({});assert.deepEqual(f.calls,['Rename']);assert.equal(f.menu.hidden,true);
 assert.equal(f.row().dataset.menuOpen,'false');
 assert.equal(f.trigger().attrs['aria-expanded'],'false');
 f.trigger().onclick();assert.equal(f.menu.children.length,5);
});
test('tap opens the same menu, Escape and outside clicks dismiss it',()=>{
 const f=fixture();f.trigger().onclick();assert.equal(f.menu.hidden,false);
 f.events.keydown({key:'Escape',preventDefault(){}});assert.equal(f.menu.hidden,true);
 f.trigger().onclick();f.events.pointerdown({target:{}});assert.equal(f.menu.hidden,true);
});
test('room changes and revoked identities prevent stale actions',()=>{
 const f=fixture();f.trigger().onclick();const action=f.menu.children[3];
 f.state.room={id:'other',status:'active'};action.onclick({});assert.deepEqual(f.calls,[]);
 f.state.room={id:'room',status:'active'};f.trigger().onclick();
 f.state.agents[0].revoked=true;f.ctx.CovenantParticipantMenu.sync();assert.equal(f.menu.hidden,true);
});
