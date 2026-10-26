const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(stored=new Map()){
  const scroll={scrollHeight:1000,scrollTop:800,clientHeight:200};
  const state={me:{username:'operator'},room:{id:'room'},cursor:12};
  const document={visibilityState:'visible'};
  const ctx=vm.createContext({state,document,$:()=>scroll,renderRooms(){},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)}});
  vm.runInContext(source.slice(source.indexOf('let roomReadCursors'),source.indexOf('$("#message-scroll").addEventListener("scroll", markCurrentRoomRead')),ctx);
  ctx.loadRoomReads();return {ctx,state,scroll,document,stored};
}
test('unread markers advance only when the current chat is visible and scrolled to the latest messages',()=>{
  const f=fixture(),room={id:'room',latest_agent_message_id:12};
  assert.equal(f.ctx.roomHasUnread(room),true);
  f.document.visibilityState='hidden';f.ctx.markCurrentRoomRead();assert.equal(f.ctx.roomHasUnread(room),true);
  f.document.visibilityState='visible';f.scroll.scrollTop=100;f.ctx.markCurrentRoomRead();assert.equal(f.ctx.roomHasUnread(room),true);
  f.scroll.scrollTop=800;f.ctx.markCurrentRoomRead();assert.equal(f.ctx.roomHasUnread(room),false);
  assert.equal(f.ctx.roomHasUnread({...room,latest_agent_message_id:13}),true);
  assert.equal(f.ctx.roomHasUnread({id:'other',latest_agent_message_id:1}),true);
  const reloaded=fixture(f.stored);assert.equal(reloaded.ctx.roomHasUnread(room),false);
  reloaded.state.me.username='another';reloaded.ctx.loadRoomReads();assert.equal(reloaded.ctx.roomHasUnread(room),true);
});
test('invalid saved cursors are ignored and read positions never move backwards',()=>{
  const f=fixture(new Map([['covenant-read:operator','{"room":-1,"other":"99"}']]));
  assert.equal(f.ctx.roomHasUnread({id:'other',latest_agent_message_id:1}),true);
  f.ctx.markCurrentRoomRead();f.state.cursor=2;f.ctx.markCurrentRoomRead();
  assert.equal(JSON.parse(f.stored.get('covenant-read:operator')).room,12);
});
