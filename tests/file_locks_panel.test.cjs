const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(options={}){
  const nodes=new Map();
  const make=(tag='',className='',textContent='')=>({tag,className,textContent,style:{},children:[],
    setAttribute(key,value){this[key]=value;},
    append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}});
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,make());return nodes.get(selector);};
  const calls=[],confirmations=[],toasts=[],state={room:{id:'room'},generation:1};
  const api=async(url,request)=>{
    calls.push({url,request});
    if(options.api)return options.api(url,request);
    return request?.method==='POST'?{released:2,file_locks:{locks:[],server_time:100}}:
      {owner:'sol',paths:['/workspace/a','/workspace/b'],expires_at:400,server_time:100};
  };
  const ctx=vm.createContext({$,state,api,element:make,senderColors:()=>({foreground:'teal'}),
    confirm:text=>{confirmations.push(text);return options.confirm!==false;},toast:text=>toasts.push(text)});
  vm.runInContext(source.slice(source.indexOf('const releasingFileLocks'),source.indexOf('function renderAgents')),ctx);
  return {$,calls,confirmations,toasts,state,render:(r,roomId)=>ctx.renderFileLocks(r,roomId)};
}
test('shows full paths, owner, server-relative expiry, and renewed reservations',()=>{
  const f=fixture(), lock={path:'/workspace/<img src=x>/review.py',owner:'sol',expires_at:400};
  f.render({locks:[lock],server_time:100,truncated:false});
  const row=f.$('#file-lock-list').children[0];
  assert.equal(row.children[0].textContent,'sol');
  assert.equal(row.children[1].textContent,lock.path);
  assert.equal(row.children[1].tag,'code');
  assert.equal(row.children[2].textContent,'Expires in 5m 0s');
  assert.equal(row.children[2].dateTime,new Date(400000).toISOString());
  assert.equal(f.$('#file-lock-count').textContent,'1');
  f.render({locks:[{...lock,expires_at:700}],server_time:100,truncated:false});
  assert.equal(f.$('#file-lock-list').children.length,1);
  assert.equal(f.$('#file-lock-list').children[0].children[2].textContent,'Expires in 10m 0s');
  f.render({locks:[],server_time:701,truncated:false});
  assert.equal(f.$('#file-lock-list').children.length,0);
  assert.equal(f.$('#file-lock-empty').hidden,false);
  assert.equal(f.$('#file-lock-empty').textContent,'No files currently reserved.');
});
test('distinguishes unavailable data from no locks and labels truncation',()=>{
  const f=fixture();f.render(undefined);
  assert.match(f.$('#file-lock-empty').textContent,/unavailable/);
  assert.equal(f.$('#file-lock-count').textContent,'');
  f.render({locks:[{path:'/workspace/a',owner:'sol',expires_at:101}],server_time:100,truncated:true});
  assert.equal(f.$('#file-lock-count').textContent,'1+');
  assert.equal(f.$('#file-lock-limit').hidden,false);
});

const releasable={path:'/workspace/a',owner:'sol',expires_at:400,lock_id:'lease-one'};
const releaseButton=f=>f.$('#file-lock-list').children[0].children.find(x=>x.tag==='button');
test('release previews all files, confirms the stale-writer risk, and removes the reservation',async()=>{
 const f=fixture();f.render({locks:[releasable],server_time:100});
 await releaseButton(f).onclick();
 assert.equal(f.calls[0].url,'/api/rooms/room/file-locks/lease-one');
 assert.equal(f.calls[1].url,'/api/rooms/room/file-locks/lease-one/release');
 assert.equal(f.calls[1].request.method,'POST');
 assert.match(f.confirmations[0],/@sol/);assert.match(f.confirmations[0],/2 file/);
 assert.ok(f.confirmations[0].includes('/workspace/b'));
 assert.match(f.confirmations[0],/300s/);assert.match(f.confirmations[0],/could continue editing/);
 assert.equal(f.$('#file-lock-list').children.length,0);
});
test('cancel and stale-lease errors leave reservations unchanged',async()=>{
 const f=fixture({confirm:false});f.render({locks:[releasable],server_time:100});
 const button=releaseButton(f);await button.onclick();assert.equal(f.calls.length,1);assert.equal(button.disabled,false);
 const stale=fixture({api:async()=>{throw new Error('Reservation changed');}});
 stale.render({locks:[releasable],server_time:100});await releaseButton(stale).onclick();
 assert.equal(stale.confirmations.length,0);assert.deepEqual(stale.toasts,['Reservation changed']);
 assert.equal(stale.$('#file-lock-list').children.length,1);
});
test('room changes and repeated clicks cannot release an unintended reservation',async()=>{
 let finish;
 const f=fixture({api:()=>new Promise(resolve=>finish=resolve)});
 f.render({locks:[releasable],server_time:100});const button=releaseButton(f);
 const pending=button.onclick();await button.onclick();assert.equal(f.calls.length,1);
 f.state.room={id:'another'};f.state.generation++;
 finish({owner:'sol',paths:['/workspace/a'],expires_at:400,server_time:100});await pending;
 assert.equal(f.confirmations.length,0);assert.equal(f.calls.length,1);
});

test('a room opened before state updates binds the release button to that room',async()=>{
 const f=fixture();
 f.render({locks:[releasable],server_time:100},'new-room');
 f.state.room={id:'new-room'};
 await releaseButton(f).onclick();
 assert.equal(f.calls[0].url,'/api/rooms/new-room/file-locks/lease-one');
 assert.equal(f.calls[1].url,'/api/rooms/new-room/file-locks/lease-one/release');
});
