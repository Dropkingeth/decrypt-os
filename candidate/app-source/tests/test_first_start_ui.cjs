// Local compiled-component fixtures. No browser, HTTP, credentials or terminal.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const {webcrypto} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const ui = path.join(root, 'app/ui');
const requireUI = createRequire(path.join(ui, 'package.json'));
const ts = requireUI('typescript');
const React = requireUI('react');
const {renderToStaticMarkup} = requireUI('react-dom/server');
const raw = fs.readFileSync(path.join(ui, 'src/FirstStartNotice.tsx'), 'utf8');
const code = ts.transpileModule(raw, {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const notice = fs.readFileSync(path.join(root, '../firstboot/NOTICE.txt'), 'utf8');
const base = {schema:'decrypt.first-start-display.v1',status:'pending',complete:false,promptRunning:false,
 policySha256:'5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b',
 documentSha256:'2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055',
 instanceId:'12345678-1234-4234-9234-123456789abc',noticeText:notice,source:'root-published-display',nativeAcceptanceEvidence:false};
function load(display, accepted=false) {
 let index=0; const values=[display,accepted,false,''];
 const module={exports:{}};
 const localReact={...React,useState:()=>[values[index++],()=>{}],useRef:value=>({current:value}),useEffect:()=>{},useCallback:fn=>fn};
 const context={module,exports:module.exports,crypto:webcrypto,TextEncoder,Uint8Array,setInterval,clearInterval,
  require:name=>name==='react'?localReact:name==='./api'?{api:()=>{throw new Error('No network allowed in fixture');}}:requireUI(name)};
 vm.runInNewContext(code,context,{filename:'FirstStartNotice.fixture.cjs'}); return module.exports;
}
(async()=>{
 const module=load(base);
 assert.equal((await module.checkedNotice(base)).status,'pending');
 await assert.rejects(module.checkedNotice({...base,noticeText:notice+' changed'}),/verified/);
 await assert.rejects(module.checkedNotice({...base,documentSha256:'0'.repeat(64)}),/changed/);
 await assert.rejects(module.checkedNotice({...base,complete:true}),/changed/);
 await assert.rejects(module.checkedNotice({...base,status:'unavailable',complete:true}),/confirmed/);
 let workspaceRenders=0;
 const workspace=React.createElement(()=>{workspaceRenders++;return React.createElement('div',null,'PRIVATE WORKSPACE');});
 let html=renderToStaticMarkup(load(base).FirstStartNotice({children:workspace}));
 assert.equal(workspaceRenders,0);assert.ok(html.includes(notice));assert.match(html,/<input type="checkbox"/);assert.match(html,/disabled=""[^>]*>Accept and open password prompt/);
 assert.ok(!html.includes('type="password"'));
 html=renderToStaticMarkup(load(base,true).FirstStartNotice({children:workspace}));
 assert.ok(!/disabled=""[^>]*>Accept and open password prompt/.test(html));
 html=renderToStaticMarkup(load({...base,status:'awaiting-password',promptRunning:true},true).FirstStartNotice({children:workspace}));
 assert.match(html,/Close password prompt/);assert.equal(workspaceRenders,0);
 html=renderToStaticMarkup(load({...base,status:'complete',complete:true}).FirstStartNotice({children:workspace}));
 assert.equal(workspaceRenders,1);assert.match(html,/PRIVATE WORKSPACE/);
 html=renderToStaticMarkup(load({schema:base.schema,status:'unavailable',complete:false,promptRunning:false,nativeAcceptanceEvidence:false}).FirstStartNotice({children:workspace}));
 assert.match(html,/Sign in to the local desktop/);assert.equal(workspaceRenders,1);
 console.log('10 first-start UI/hash/render assertions passed; no native browser proof.');
})().catch(error=>{console.error(error);process.exitCode=1;});
