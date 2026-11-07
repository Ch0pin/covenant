const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/swipe-reply.js'),'utf8');
function classes(){const set=new Set();return {add:(...c)=>c.forEach(x=>set.add(x)),remove:(...c)=>c.forEach(x=>set.delete(x)),toggle:(c,on)=>on?set.add(c):set.delete(c),contains:c=>set.has(c)};}
function fixture({overflowX='visible',separateArea=false}={}){
 const listeners={},props={},timers=[];
 const target={dataset:{},classList:classes(),captured:null,
  style:{transform:'',setProperty:(k,v)=>props[k]=v,removeProperty:k=>delete props[k]},
  addEventListener:(name,fn)=>(listeners[name]||=[]).push(fn),setPointerCapture(id){this.captured=id;}};
 const area=separateArea?{captured:null,addEventListener:(name,fn)=>(listeners[name]||=[]).push(fn),setPointerCapture(id){this.captured=id;}}:target;
 if(separateArea)target.addEventListener=()=>{throw new Error('listeners belong on the message');};
 const body={classList:classes()},vibrations=[];
 const ctx=vm.createContext({document:{body},navigator:{vibrate:ms=>vibrations.push(ms)},setTimeout:fn=>timers.push(fn),getComputedStyle:()=>({overflowX})});
 vm.runInContext(source,ctx);
 let replies=0;ctx.CovenantSwipeReply.attach(target,()=>replies++,separateArea?area:undefined);
 const fire=(name,props={})=>{const event={pointerId:1,isPrimary:true,pointerType:'touch',target:{closest:()=>null},defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){},...props,type:name};for(const fn of listeners[name]||[])fn(event);return event;};
 const swipe=(points,end='pointerup',extra={})=>{fire('pointerdown',{clientX:100,clientY:100,...extra});for(const [x,y] of points)fire('pointermove',{clientX:x,clientY:y,...extra});return fire(end,{clientX:points.at(-1)[0],clientY:points.at(-1)[1],...extra});};
 return {target,area,body,props,vibrations,timers,fire,swipe,replies:()=>replies};
}
test('swiping a message right or left past the threshold selects it for reply',()=>{
 for(const direction of [1,-1]){
  const f=fixture();
  f.fire('pointerdown',{clientX:100,clientY:100});
  f.fire('pointermove',{clientX:100+20*direction,clientY:102});
  assert.ok(f.target.classList.contains('swiping'));assert.ok(f.body.classList.contains('swiping-reply'));
  assert.equal(f.target.dataset.swipe,direction>0?'right':'left');
  f.fire('pointermove',{clientX:100+200*direction,clientY:104});
  // Movement is capped and the ready state gives one haptic tick.
  assert.equal(f.target.style.transform,`translateX(${88*direction}px)`);
  assert.ok(f.target.classList.contains('swipe-ready'));assert.deepEqual(f.vibrations,[10]);
  f.fire('pointerup',{clientX:100+200*direction,clientY:104});
  assert.equal(f.replies(),1);
  // The bubble springs back and no styling is left behind.
  assert.equal(f.target.style.transform,'');assert.equal(f.target.dataset.swipe,undefined);
  assert.ok(!f.target.classList.contains('swiping'));assert.ok(!f.body.classList.contains('swiping-reply'));
  assert.equal(f.props['--swipe-progress'],undefined);
 }
});
test('short swipes, vertical scrolling and cancelled gestures do not reply',()=>{
 let f=fixture();f.swipe([[130,101],[150,102]]);assert.equal(f.replies(),0);assert.equal(f.target.style.transform,'');
 f=fixture();f.swipe([[103,130],[180,200]]);assert.equal(f.replies(),0);assert.equal(f.target.style.transform,'');
 f=fixture();f.swipe([[130,101],[200,102]],'pointercancel');assert.equal(f.replies(),0);
});
test('mouse drags, secondary pointers and typing fields keep their normal behaviour',()=>{
 let f=fixture();f.swipe([[130,100],[200,100]],'pointerup',{pointerType:'mouse'});
 assert.equal(f.replies(),0);assert.ok(!f.target.classList.contains('swiping'));
 f=fixture();f.swipe([[130,100],[200,100]],'pointerup',{isPrimary:false});assert.equal(f.replies(),0);
 f=fixture();f.swipe([[130,100],[200,100]],'pointerup',{target:{closest:selector=>selector.includes('textarea')?{tag:'textarea'}:null}});assert.equal(f.replies(),0);
});
test('a swipe can start on links, code and other parts of the message',()=>{
 const link={closest:selector=>selector.includes('textarea')?null:{tag:'a'},scrollWidth:40,clientWidth:40,parentElement:null};
 const f=fixture();f.swipe([[130,100],[200,100]],'pointerup',{target:link});assert.equal(f.replies(),1);
});
test('code blocks that scroll sideways keep their own gesture',()=>{
 const f=fixture({overflowX:'auto'});
 const code={closest:()=>null,scrollWidth:900,clientWidth:300,parentElement:null};
 f.swipe([[130,100],[200,100]],'pointerup',{target:code});assert.equal(f.replies(),0);
 const short={closest:()=>null,scrollWidth:300,clientWidth:300,parentElement:null};
 f.swipe([[130,100],[200,100]],'pointerup',{target:short});assert.equal(f.replies(),1);
});
test('the whole message listens while the bubble slides, and a reply in a thread wins over its parent',()=>{
 const f=fixture({separateArea:true});
 f.fire('pointerdown',{clientX:100,clientY:100});f.fire('pointermove',{clientX:180,clientY:100});
 assert.equal(f.target.style.transform,'translateX(80px)');assert.equal(f.area.captured,1);
 f.fire('pointerup',{clientX:180,clientY:100});assert.equal(f.replies(),1);
 const claimed=fixture();claimed.swipe([[130,100],[200,100]],'pointerup',{swipeClaimed:true});assert.equal(claimed.replies(),0);
});
test('the click that ends a swipe is swallowed, later taps are not',()=>{
 const f=fixture();f.swipe([[130,100],[200,100]]);
 assert.equal(f.fire('click').defaultPrevented,true);
 assert.equal(f.fire('click').defaultPrevented,false);
});
test('a failed pointer capture does not break the swipe',()=>{
 const f=fixture();f.target.setPointerCapture=()=>{throw new Error('No active pointer');};
 f.fire('pointerdown',{clientX:100,clientY:100});f.fire('pointermove',{clientX:180,clientY:100});
 assert.ok(f.target.classList.contains('swiping'));
 f.fire('pointerup',{clientX:180,clientY:100});assert.equal(f.replies(),1);
});
