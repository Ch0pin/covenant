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
test('the stronger marker is reserved for unread messages addressed to the operator',()=>{
  const f=fixture();
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:12,latest_operator_message_id:0}),false);
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:12,latest_operator_message_id:12}),true);
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:12}),false);
  f.ctx.markCurrentRoomRead();
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:12,latest_operator_message_id:12}),false);
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:13,latest_operator_message_id:13}),true);
  assert.equal(f.ctx.roomHasUnreadForOperator({id:'room',latest_agent_message_id:13,latest_operator_message_id:5}),false);
});
test('invalid saved cursors are ignored and read positions never move backwards',()=>{
  const f=fixture(new Map([['covenant-read:operator','{"room":-1,"other":"99"}']]));
  assert.equal(f.ctx.roomHasUnread({id:'other',latest_agent_message_id:1}),true);
  f.ctx.markCurrentRoomRead();f.state.cursor=2;f.ctx.markCurrentRoomRead();
  assert.equal(JSON.parse(f.stored.get('covenant-read:operator')).room,12);
});

function roomSelectionFixture(){
 const state={me:{username:'operator'},archived:false,room:{id:'active',status:'active'},rooms:[],generation:1};
 let rows=[],cleared=0;
 const ctx=vm.createContext({state,api:async()=>({rooms:rows}),renderRooms(){},closeRoomMenu(){},
  clearConversation(){cleared++;state.room=null;state.generation++;},
  $:()=>({classList:{toggle(){}},setAttribute(){}})});
 vm.runInContext(source.slice(source.indexOf('async function refreshRooms()'),source.indexOf('$("#active-tab").onclick')),ctx);
 return {ctx,state,setRows(value){rows=value;},get cleared(){return cleared;}};
}
test('switching filters clears an invisible selection and invalidates old room loads',async()=>{
 const f=roomSelectionFixture();f.setRows([{id:'archive',status:'archived'}]);
 await f.ctx.setFilter(true);
 assert.equal(f.state.room,null);assert.equal(f.cleared,1);assert.ok(f.state.generation>1);
});
test('room refresh clears a removed room and preserves a room moved into the selected filter',async()=>{
 const f=roomSelectionFixture();f.setRows([]);await f.ctx.refreshRooms();assert.equal(f.state.room,null);
 f.state.room={id:'archive',status:'archived'};f.setRows([f.state.room]);await f.ctx.setFilter(true);
 assert.equal(f.state.room.id,'archive');assert.equal(f.cleared,1);
});
