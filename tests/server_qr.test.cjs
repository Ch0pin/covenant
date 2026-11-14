const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const dir=path.join(__dirname,'..','covenant','static');
function fixture(){
 const rects=[],canvas={width:0,height:0,attrs:{},setAttribute(k,v){this.attrs[k]=v;},getContext(){return{fillStyle:'',fillRect(x,y,w,h){rects.push([this.fillStyle,x,y,w,h]);}};}};
 const box={hidden:true,dataset:{}},link={textContent:'',href:''},listeners={};
 const nodes={'#server-qr':box,'#server-qr-code':canvas,'#server-qr-address':link};
 const context={state:{me:null},document:{querySelector:s=>nodes[s]},window:{addEventListener:(n,f)=>listeners[n]=f}};
 context.globalThis=context;vm.createContext(context);
 vm.runInContext(fs.readFileSync(path.join(dir,'qrcode-generator.js'),'utf8'),context);
 vm.runInContext(fs.readFileSync(path.join(dir,'server-qr.js'),'utf8'),context);
 return{context,box,canvas,link,rects,login:()=>listeners['covenant-login-change']()};
}
test('settings shows the server address as a QR code after sign-in and hides it after sign-out',()=>{
 const f=fixture();
 f.login();assert.equal(f.box.hidden,true);assert.equal(f.rects.length,0);
 f.context.state.me={public_base_url:'https://amy-weight-reliability-pets.trycloudflare.com'};f.login();
 assert.equal(f.box.hidden,false);
 assert.equal(f.link.textContent,'https://amy-weight-reliability-pets.trycloudflare.com');
 assert.equal(f.link.href,f.link.textContent);
 assert.match(f.canvas.attrs['aria-label'],/trycloudflare\.com/);
 const {count}=f.context.CovenantServerQr.modules(f.link.textContent),scale=f.canvas.width/(count+8);
 assert.ok(Number.isInteger(scale)&&scale>=1&&f.canvas.width<=126&&f.canvas.width===f.canvas.height);
 assert.deepEqual(f.rects[0],['#fff',0,0,f.canvas.width,f.canvas.width]);
 const dark=f.rects.slice(1);
 assert.ok(dark.length>100&&dark.every(r=>r[0]==='#000'&&r[1]>=4*scale&&r[2]>=4*scale&&r[1]+r[3]<=f.canvas.width-4*scale));
 // the three finder patterns put a dark module in each of these corners
 for(const [row,column] of [[0,0],[0,count-1],[count-1,0]]) assert.ok(dark.some(r=>r[1]===(column+4)*scale&&r[2]===(row+4)*scale));
 const drawn=f.rects.length;f.login();assert.equal(f.rects.length,drawn);
 f.context.state.me={public_base_url:'https://new-address.example.com'};f.login();
 assert.ok(f.rects.length>drawn);assert.equal(f.link.textContent,'https://new-address.example.com');
 f.context.state.me=null;f.login();assert.equal(f.box.hidden,true);
});
test('the page loads the QR library before the module that draws it',()=>{
 const html=fs.readFileSync(path.join(dir,'index.html'),'utf8');
 assert.ok(html.indexOf('/static/qrcode-generator.js')>0&&html.indexOf('/static/qrcode-generator.js')<html.indexOf('/static/server-qr.js'));
 assert.match(html,/id="workspace-settings-menu"[\s\S]*id="server-qr"[\s\S]*<\/section>/);
});
