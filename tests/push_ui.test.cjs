const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=name=>fs.readFileSync(path.join(__dirname,'../covenant/static/'+name),'utf8');
const room='11111111-1111-1111-1111-111111111111',id='22222222-2222-2222-2222-222222222222';
function workerFixture(){
 const events={},shown=[],opened=[],posted=[];let rows=[{id,status:'pending'}],status=200;
 const self={addEventListener:(n,f)=>events[n]=f,skipWaiting:async()=>{},location:{origin:'https://chat.example.com'},registration:{showNotification:async(...args)=>shown.push(args)},clients:{claim:async()=>{},matchAll:async()=>[],openWindow:async url=>opened.push(url)}};
 const ctx=vm.createContext({self,URL,Date,AbortSignal,fetch:async()=>({status,ok:status===200,json:async()=>({requests:rows})})});
 vm.runInContext(source('service-worker.js'),ctx);
 const data={kind:'pending',room_id:room,approval_id:id,expires_at:Date.now()/1000+600};
 async function push(p=data){let promise;events.push({data:{json:()=>p},waitUntil:p=>promise=p});await promise;}
 return{events,shown,opened,posted,self,data,push,setRows:r=>rows=r,setStatus:s=>status=s};
}
test('background alerts use generic text and suppress ended, stale, and logged-out requests',async()=>{
 const f=workerFixture();await f.push();assert.equal(f.shown.length,1);assert.equal(f.shown[0][1].tag,'approval-'+id);
 assert.equal(f.shown[0][1].body,'An agent needs your decision. Tap to review.');
 f.setRows([{id,status:'resolved'}]);await f.push();assert.equal(f.shown.length,1);
 f.setRows([{id,status:'expired'}]);await f.push({...f.data,kind:'expired'});assert.equal(f.shown.length,2);
 f.setStatus(401);await f.push();assert.equal(f.shown.length,2);
 await f.push({...f.data,expires_at:1});assert.equal(f.shown.length,2);
 await f.push({...f.data,room_id:'https://evil.example'});assert.equal(f.shown.length,2);
});
test('notification taps open the approval route or focus an existing window',async()=>{
 const f=workerFixture();let promise,closed=0;
 const event=()=>({notification:{close:()=>closed++,data:{room_id:room,approval_id:id}},waitUntil:p=>promise=p});
 f.events.notificationclick(event());await promise;assert.equal(f.opened[0],`/${room}?approval=${id}`);
 let focused=false;f.self.clients.matchAll=async()=>[{url:'https://chat.example.com/',focus:async()=>focused=true,postMessage:data=>f.posted.push(data)}];
 f.events.notificationclick(event());await promise;assert.equal(focused,true);assert.equal(f.posted[0].approval_id,id);assert.equal(closed,2);
});
function pageFixture(){
 const listeners={},calls=[],saved=new Map(),button={setAttribute(){}};
 let permission='default',sub=null;
 const subscription={endpoint:'https://fcm.googleapis.com/test',options:{},toJSON(){return{endpoint:this.endpoint,keys:{}};},unsubscribe:async()=>{calls.push('browser-unsubscribe');sub=null;return true;}};
 const reg={pushManager:{getSubscription:async()=>sub,subscribe:async options=>{calls.push('subscribe');assert.equal(options.userVisibleOnly,true);sub=subscription;return sub;}}};
 const state={me:{username:'operator'}};
 const navigator={serviceWorker:{register:async()=>{calls.push('register');return reg;},ready:Promise.resolve(reg),addEventListener:(n,f)=>listeners['sw-'+n]=f}};
 const Notification={get permission(){return permission;},requestPermission:async()=>{calls.push('permission');permission='granted';return permission;}};
 const context=vm.createContext({state,navigator,Notification,window:{isSecureContext:true,PushManager:{},Notification,addEventListener:(n,f)=>listeners[n]=f},$:()=>button,localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},location:{search:'',pathname:'/'},URLSearchParams,Uint8Array,atob,toast:()=>{},api:async(path,options)=>{calls.push(path);if(path==='/api/push/config')return{public_key:'BAECAw'};return{ok:true};}});
 vm.runInContext(source('push.js'),context);
 return{button,calls,context,state,listeners,saved};
}
test('mobile opt-in requests permission directly, registers server subscription, and disables both ends',async()=>{
 const f=pageFixture();assert.equal(f.calls.length,0);
 await f.button.onclick();assert.equal(f.calls[0],'permission');assert.ok(f.calls.includes('/api/push/subscriptions'));
 assert.equal(f.context.CovenantPush.active,true);assert.equal(f.button.textContent,'Disable approval alerts');
 await f.button.onclick();assert.ok(f.calls.indexOf('/api/push/unsubscribe')<f.calls.indexOf('browser-unsubscribe'));
 assert.equal(f.context.CovenantPush.active,false);
});
test('logout clears the local active push indicator',async()=>{
 const f=pageFixture();await f.button.onclick();f.state.me=null;f.listeners['covenant-login-change']();
 assert.equal(f.context.CovenantPush.active,false);
});
