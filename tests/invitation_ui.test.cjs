const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(){
 const nodes=new Map(),calls=[],copies=[];
 function $(id){
  if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',checked:false,disabled:false,open:false,listeners:{},
   addEventListener(type,fn){this.listeners[type]=fn;},
   showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();},
   focus(){},select(){}});
  return nodes.get(id);
 }
 const data={handle:'alice',instructions:'Room: https://chat.example.com/room\nToken: secret-A\nMCP rules',http_instructions:'\n\nHTTP fallback\nHTTP rules'};
 const ctx=vm.createContext({$,state:{room:{id:'room'}},api:async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return data;},
  refreshCurrent:async()=>{},FormData:class {constructor(form){this.entries=form.entries;}[Symbol.iterator](){return this.entries[Symbol.iterator]();}},
  navigator:{clipboard:{writeText:async value=>copies.push(value)}},toast(){}});
 vm.runInContext(source.slice(source.indexOf('let currentInvite ='),source.indexOf('async function revokeAgent')),ctx);
 vm.runInContext('$("#invite-dialog").addEventListener("close", clearInvite);',ctx);
 return {$,calls,copies,data,show:ctx.showInvite,ctx};
}
test('MCP is the default and toggling HTTP changes only the preview and copied text',async()=>{
 const f=fixture();f.show(f.data);
 assert.equal(f.$('#invite-include-http').checked,false);
 assert.equal(f.$('#invite-text').value,f.data.instructions);
 await f.$('#copy-invite').onclick();assert.equal(f.copies[0],f.data.instructions);
 f.$('#invite-include-http').checked=true;f.$('#invite-include-http').listeners.change();
 assert.equal(f.$('#invite-text').value,f.data.instructions+f.data.http_instructions);
 assert.equal(f.$('#copy-invite').textContent,'Copy connection instructions');
 await f.$('#copy-invite').onclick();assert.equal(f.copies[1],f.data.instructions+f.data.http_instructions);
 f.$('#invite-include-http').checked=false;f.$('#invite-include-http').listeners.change();
 assert.equal(f.$('#invite-text').value,f.data.instructions);assert.equal(f.calls.length,0);
});
test('creation checkbox selects HTTP without adding a field to the agent API request',async()=>{
 const f=fixture();f.$('#agent-include-http').checked=true;
 const button={},error={},form={entries:[['handle','alice']],querySelector:s=>s.includes('submit')?button:error};
 await f.$('#agent-form').listeners.submit({preventDefault(){},currentTarget:form});
 assert.deepEqual(f.calls,[{url:'/api/rooms/room/agents',body:{handle:'alice'}}]);
 assert.equal(f.$('#invite-include-http').checked,true);
 assert.equal(f.$('#invite-text').value,f.data.instructions+f.data.http_instructions);
 assert.equal(button.disabled,false);
});
test('renewed tickets reset to MCP and closing discards all invitation text',()=>{
 const f=fixture();f.show(f.data,true);
 const renewed={...f.data,instructions:'Token: secret-B\nNew MCP rules'};
 f.show(renewed);
 assert.equal(f.$('#invite-include-http').checked,false);
 assert.equal(f.$('#invite-text').value,renewed.instructions);
 f.$('#invite-dialog').close();assert.equal(f.$('#invite-text').value,'');
 assert.equal(vm.runInContext('currentInvite',f.ctx),null);
 f.$('#invite-include-http').checked=true;f.$('#invite-include-http').listeners.change();
 assert.equal(f.$('#invite-text').value,'');
});
test('an older server preserves its ticket and explains that the format option needs a restart',()=>{
 const f=fixture();f.show({handle:'alice',instructions:'Original legacy invitation'},true);
 assert.equal(f.$('#invite-text').value,'Original legacy invitation');
 assert.equal(f.$('#invite-include-http').disabled,true);
 assert.match(f.$('#invite-include-http').title,/Restart Covenant/);
});
