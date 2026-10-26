const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(){
  const make=(tag,cls='',text='')=>({tag,className:cls,textContent:text,dataset:{},style:{},attrs:{},children:[],setAttribute(k,v){this.attrs[k]=v;},append(n){this.children.push(n);}});
  const state={agents:[]}, nodes=[];
  const ctx=vm.createContext({state,element:make,document:{createElementNS:(_,tag)=>make(tag)},senderColors:()=>({foreground:'teal',background:'black'}),$:()=>({querySelectorAll:()=>nodes})});
  vm.runInContext(source.slice(source.indexOf('const avatarSymbols'),source.indexOf('function updateMessageReceipts')),ctx);
  return {ctx,state,nodes};
}
test('renders all five local symbols with initials fallback and operator identity preserved',()=>{
  const f=fixture();
  for(const avatar_id of ['raven','moon','key','eye','flame']){
    f.state.agents=[{handle:'sol',avatar_id}];const a=f.ctx.senderAvatar('sol');
    assert.equal(a.dataset.avatar,avatar_id);assert.equal(a.children[0].tag,'svg');
    assert.equal(a.style.color,'teal');assert.match(a.title,new RegExp(avatar_id));
  }
  for(const avatar_id of [undefined,'initials','https://example.com/avatar','__proto__']){
    f.state.agents=[{handle:'sol',avatar_id}];const a=f.ctx.senderAvatar('sol');
    assert.equal(a.textContent,'SO');assert.equal(a.children.length,0);
  }
  f.state.agents=[{handle:'operator',avatar_id:'raven'}];assert.equal(f.ctx.senderAvatar('operator').textContent,'OP');
});
test('updates an existing message avatar when its participant changes selection',()=>{
  const f=fixture();let replacement;
  f.nodes.push({dataset:{sender:'sol',avatar:'initials'},replaceWith(n){replacement=n;}});
  f.state.agents=[{handle:'sol',avatar_id:'raven'}];f.ctx.refreshMessageAvatars();
  assert.equal(replacement.dataset.avatar,'raven');
});
