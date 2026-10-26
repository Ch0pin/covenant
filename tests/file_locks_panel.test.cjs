const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../covenant/static/app.js'),'utf8');
function fixture(){
  const nodes=new Map();
  const make=(tag='',className='',textContent='')=>({tag,className,textContent,style:{},children:[],
    append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}});
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,make());return nodes.get(selector);};
  const ctx=vm.createContext({$,element:make,senderColors:()=>({foreground:'teal'})});
  vm.runInContext(source.slice(source.indexOf('function renderFileLocks'),source.indexOf('function renderAgents')),ctx);
  return {$,render:r=>ctx.renderFileLocks(r)};
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
