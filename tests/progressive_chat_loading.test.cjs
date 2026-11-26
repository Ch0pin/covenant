const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../covenant/static/app.js'),'utf8');
const selection=source.slice(source.indexOf('async function selectRoom'),source.indexOf('function clearMessageHighlight'));

function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};}
function tick(){return new Promise(resolve=>setImmediate(resolve));}
function fixture(rooms=[{id:'a',title:'A',status:'active',latest_operator_message_id:0}]){
  class Node{
    constructor(){this.hidden=false;this.disabled=false;this.textContent='';this.className='';this.children=[];this.classList={add(){}};}
    replaceChildren(...children){this.children=children;}
    querySelector(){return null;}
    getBoundingClientRect(){return{top:0};}
    focus(){}
  }
  const nodes=new Map(),calls=[],pages=[];
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,new Node());return nodes.get(selector);};
  const roomReadCursors={a:0,b:0},stored={};
  const state={me:{username:'operator'},rooms,room:null,agents:[],cursor:0,first:0,generation:0,messageEpoch:0,messagesReady:false,messageLoadError:false,preserveUnread:false,unreadAcknowledged:false,jumpLoading:false,pending:null};
  const handlers=[];
  const api=url=>{calls.push(url);const handler=handlers.shift();if(!handler)throw new Error(`unexpected ${url}`);return handler(url);};
  const context=vm.createContext({state,roomReadCursors,FIRST_PAGE:10,api,$,Object,Promise,document:{visibilityState:'visible'},
    localStorage:{setItem(key,value){stored[key]=value;}},
    hideMentions(){},closeRoomMenu(){},clearReplyTarget(){},resetComposer(){},renderFileLocks(){},renderAgents(){},renderRooms(){},applyPanelPreferences(){},applyPanelWidths(){},
    history:{replaceState(){}},globalThis:{CovenantMobile:{roomSelected(){}}},refreshMessageAvatars(){},updateMentions(){},toast(){},updateScrollBottomButton(){},
    updateRoom(){ $('#no-messages h2').textContent='The room is ready.'; },
    appendMessages(messages){pages.push(messages.map(message=>message.id));$('#no-messages').hidden=messages.length>0;},scrollBottom(){}});
  vm.runInContext(selection,context);
  vm.runInContext(source.slice(source.indexOf('function markCurrentRoomRead'),source.indexOf('$("#message-scroll").addEventListener("scroll", markCurrentRoomRead')),context);
  return{context,state,roomReadCursors,stored,nodes,calls,pages,queue(...items){handlers.push(...items.map(item=>typeof item==='function'?item:()=>item));},$};
}
const details=(id='a',latest=0)=>({id,title:id.toUpperCase(),status:'active',agents:[],latest_operator_message_id:latest});
const page=(id=10)=>({messages:[{id}],next_cursor:id,has_older:true});

test('latest ten render while room details remain deferred',async()=>{
  const f=fixture(),slow=deferred();f.queue(()=>slow.promise,async()=>page(10));
  const pending=f.context.selectRoom('a');await tick();
  assert.deepEqual(f.pages,[[10]]);assert.equal(f.state.messagesReady,true);assert.equal(f.$('#no-messages').hidden,true);
  const scroll=f.$('#message-scroll');scroll.scrollHeight=1000;scroll.scrollTop=800;scroll.clientHeight=200;
  f.context.markCurrentRoomRead();assert.equal(f.roomReadCursors.a,0);assert.equal(f.stored['covenant-read:operator'],undefined);
  slow.resolve(details());await pending;
});

test('navigation ignores stale detail and message responses',async()=>{
  const f=fixture([{id:'a',title:'A',status:'active',latest_operator_message_id:0},{id:'b',title:'B',status:'active',latest_operator_message_id:0}]);
  const aDetails=deferred(),aMessages=deferred();
  f.queue(()=>aDetails.promise,()=>aMessages.promise,async()=>details('b'),async()=>page(20));
  const first=f.context.selectRoom('a'),second=f.context.selectRoom('b');await second;
  aMessages.resolve(page(10));aDetails.resolve(details('a'));await first;
  assert.equal(f.state.room.id,'b');assert.deepEqual(f.pages,[[20]]);
});

test('both response orders preserve authoritative unread state and loading copy',async()=>{
  for(const detailsFirst of [true,false]){
    const f=fixture(),room=deferred(),messages=deferred();f.queue(()=>room.promise,()=>messages.promise);
    const pending=f.context.selectRoom('a');
    if(detailsFirst){room.resolve(details('a',12));await tick();assert.equal(f.$('#no-messages h2').textContent,'Loading messages…');messages.resolve(page(12));}
    else{messages.resolve(page(12));await tick();room.resolve(details('a',12));}
    await pending;assert.equal(f.state.preserveUnread,true);assert.equal(f.$('#jump-first-unread').hidden,false);
  }
});

test('late room details cannot restore unread after a completed jump',async()=>{
  const f=fixture([{id:'a',title:'A',status:'active',latest_operator_message_id:12}]),room=deferred();
  f.queue(()=>room.promise,async()=>page(12),async()=>({...page(12),focus_id:null}));
  const selection=f.context.selectRoom('a');await tick();
  await f.context.jumpFirstUnread();
  assert.equal(f.state.preserveUnread,false);assert.equal(f.state.unreadAcknowledged,true);
  room.resolve(details('a',12));await selection;
  assert.equal(f.state.preserveUnread,false);assert.equal(f.$('#jump-first-unread').hidden,true);
});

test('failed initial messages expose a retry that restores polling state',async()=>{
  const f=fixture();f.queue(async()=>details(),async()=>{throw new Error('offline');},async()=>page(10));
  await assert.rejects(f.context.selectRoom('a'),/offline/);
  assert.equal(f.$('#retry-messages').hidden,false);assert.equal(f.state.messagesReady,false);
  f.$('#retry-messages').onclick();await tick();
  assert.equal(f.state.messagesReady,true);assert.equal(f.$('#retry-messages').hidden,true);assert.deepEqual(f.pages,[[10]]);
});
