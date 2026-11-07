const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(){
  const nodes=new Map();
  const $=s=>{if(!nodes.has(s))nodes.set(s,{value:'',hidden:true,textContent:'',focus(){this.focused=true;}});return nodes.get(s);};
  const state={room:{id:'one',status:'active'},agents:[{handle:'sol',revoked:false}],pending:{},replyTo:null};
  const ctx=vm.createContext({$,state,composerRecipients:[],syncComposer(){}});
  vm.runInContext(source.slice(source.indexOf('function clearReplyTarget'),source.indexOf('async function loadThread')),ctx);
  return {$,state,ctx};
}
test('reply targets prefill the recipient, preserve drafts, and cancel without deleting text',()=>{
  const f=fixture();f.ctx.setReplyTarget({id:2,sender:'sol'});
  assert.equal(f.state.replyTo.id,2);assert.equal(f.$('#message-text').value,'@sol ');
  assert.equal(f.$('#reply-context').hidden,false);assert.equal(f.state.pending,null);
  f.$('#message-text').value='@operator Keep this draft';
  f.ctx.setReplyTarget({id:3,sender:'operator',targets:['everyone']});
  assert.equal(f.$('#message-text').value,'@operator Keep this draft');
  assert.equal(f.state.replyTo.id,3);
  f.$('#cancel-reply').onclick();
  assert.equal(f.state.replyTo,null);assert.equal(f.$('#reply-context').hidden,true);
  assert.equal(f.$('#message-text').value,'@operator Keep this draft');
});
test('cancelling a reply removes the recipient it filled in but keeps what the operator wrote',()=>{
  // The recipient is still plain text in the box.
  let f=fixture();f.ctx.setReplyTarget({id:2,sender:'sol'});
  f.$('#cancel-reply').onclick();
  assert.equal(f.$('#message-text').value,'');assert.equal(f.state.replyTo,null);
  // The recipient became a chip, the operator typed a message and picked another recipient.
  f=fixture();f.ctx.setReplyTarget({id:2,sender:'sol'});
  f.ctx.composerRecipients.push('sol','everyone');f.$('#message-text').value='Please rerun the tests';
  f.$('#cancel-reply').onclick();
  assert.deepEqual([...f.ctx.composerRecipients],['everyone']);assert.equal(f.$('#message-text').value,'Please rerun the tests');
  // The operator already removed the filled-in recipient; nothing else is touched.
  f=fixture();f.ctx.setReplyTarget({id:2,sender:'sol'});
  f.ctx.composerRecipients.push('everyone');f.$('#message-text').value='@solar notes';
  f.$('#cancel-reply').onclick();
  assert.deepEqual([...f.ctx.composerRecipients],['everyone']);assert.equal(f.$('#message-text').value,'@solar notes');
});
test('replies to own messages use their recipients, and archives prevent reply selection',()=>{
  const f=fixture();f.ctx.setReplyTarget({id:1,sender:'operator',targets:['sol']});
  assert.equal(f.$('#message-text').value,'@sol ');
  f.ctx.clearReplyTarget();f.$('#message-text').value='';f.state.room.status='archived';
  f.ctx.setReplyTarget({id:2,sender:'sol'});
  assert.equal(f.state.replyTo,null);assert.equal(f.$('#message-text').value,'');
});
