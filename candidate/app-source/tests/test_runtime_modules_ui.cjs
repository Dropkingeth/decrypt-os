// Actual React/runtime API flow. Every API and provider-like response is a fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {pathToFileURL}=require('node:url');
const root=path.resolve(__dirname,'..'),ui=path.join(root,'app/ui');
const out=process.env.MODULE_UI_EVIDENCE||path.resolve(root,'ui-evidence/modules-ui');
const playwright=require(process.env.PLAYWRIGHT_MODULE);
const now=Date.now(),instanceId='engine-fixture',pairId='fixture-pair',engineSha256='1'.repeat(64),strategySha256='2'.repeat(64);
let count=0;const check=(yes,label)=>{assert.ok(yes,label);count++};
(async()=>{
 const {createServer}=await import(pathToFileURL(path.join(ui,'node_modules/vite/dist/node/index.js')).href);
 const server=await createServer({root:ui,configFile:path.join(ui,'vite.config.ts'),server:{host:'127.0.0.1',port:8854,strictPort:true,fs:{allow:[root]}},
  resolve:{alias:{react:path.join(ui,'node_modules/react'),'react-dom':path.join(ui,'node_modules/react-dom')}},
  plugins:[{name:'module-fixture',configureServer(s){s.middlewares.use('/module-fixture',async(req,res,next)=>{try{
   res.setHeader('Content-Type','text/html');res.end(await s.transformIndexHtml('/module-fixture','<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main id="root" style="max-width:1160px;margin:0 auto;padding:24px"></main><script type="module" src="/@fs'+path.join(root,'tests/runtime_modules_entry.tsx')+'"></script></body></html>'));
  }catch(e){next(e)}})}}]});await server.listen();
 const browser=await playwright.chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try{
  fs.mkdirSync(out,{recursive:true});const page=await browser.newPage({viewport:{width:1280,height:1100}}),errors=[],requests=[];
  await page.addInitScript(value=>{window.fixtureClock=value;Date.now=()=>window.fixtureClock},now);
  page.setDefaultTimeout(10000);page.on('pageerror',error=>errors.push(String(error)));
  const pkg={pairId,label:'Fixture engine',version:'fixture-1',engineId:'fixture',publicationVerified:false,providerCommissioned:false,tp1OffsetMode:'zero'};
  const instance={instanceId,runtimeState:'running',entriesHeld:true,entryReady:false,nativeFresh:true,tradePhase:'closed',pendingCommands:0,blockers:['publication_not_verified'],binding:{accountId:'fixture-account',symbol:'MNQZ2026',pairId,engineSha256,strategySha256,streamId:'fixture-stream',feedId:'fixture-feed'}};
  const module={instanceId,moduleId:'guardian-observer',version:'native-confirmation-v1',revision:1,pairId,bindingSha256:'3'.repeat(64),lifecycleState:'installed',configuration:{entryMaxAgeMs:12000},origin:'engine_install',createdAt:now-1000,updatedAt:now-1000,sourceCurrent:true,implementationSha256:'4'.repeat(64),runtimeState:'running',observationState:'fresh',observationReason:null,observedAt:now,entriesHeld:true,entryAgeAllowed:true,engineRetired:false,requiredForEntry:true};
  const state={enabled:true,mode:'customer_demo_only',package:pkg,packages:[pkg],instances:[instance],retiredInstances:[],account:null,health:[],events:[],routes:{locked:false},ingress:{state:'listening_loopback',publicTlsVerified:false},ingressKeys:{locked:false,producers:[{instanceId,channel:'pine',generation:1}]},moduleCatalog:[{moduleId:'guardian-observer',version:'native-confirmation-v1',label:'Guardian runtime confirmation',requiredForEntry:true,compatiblePairs:[{pairId,engineSha256,strategySha256}],defaultConfiguration:{entryMaxAgeMs:12000},entryMaxAgeMs:{minimum:1000,maximum:12000}}],operationalModules:[module]};
  let refuseModule=false,loseRotation=false;
  const address=()=>({instanceId,url:'https://fixture.example/signal/'+instanceId+'/FAKE-GENERATION-'+state.ingressKeys.producers[0].generation,generation:state.ingressKeys.producers[0].generation});
  await page.route('**/api/**',async route=>{
   const req=route.request(),url=new URL(req.url()),body=req.method()==='POST'?req.postDataJSON():null;requests.push({path:url.pathname,body});
   const reply=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
   if(url.pathname==='/api/session')return reply({csrfToken:'fixture-csrf'});
   if(url.pathname==='/api/trading/status')return reply(state);
   if(url.pathname.endsWith('/configure-module')){module.revision++;module.configuration=body.configuration;module.observationState='unknown';module.observationReason='awaiting_post_change_observation';module.entryAgeAllowed=false;return reply(state)}
   if(url.pathname.endsWith('/retire-module')){if(refuseModule)return reply({error:{code:'module_settled_required',message:'The current trade still needs Guardian supervision.'}},409);module.revision++;module.lifecycleState='retired';module.runtimeState='retired';module.observationState='unknown';module.observationReason='module_retired';return reply(state)}
   if(url.pathname.endsWith('/install-module')){module.revision++;module.lifecycleState='installed';module.runtimeState='running';module.observationState='unknown';module.observationReason='awaiting_post_change_observation';return reply(state)}
   if(url.pathname.endsWith('/show-signal-address'))return reply({...state,oneTimeIngress:address()});
   if(url.pathname.endsWith('/rotate-signal-key')){if(body.expectedGeneration!==state.ingressKeys.producers[0].generation)return reply({error:{code:'ingress_generation_changed',message:'The saved key changed. Refresh its generation.'}},409);state.ingressKeys.producers[0].generation++;if(loseRotation)return route.abort('connectionreset');return reply({...state,oneTimeIngress:address()})}
   return reply({error:{code:'fixture_route',message:'Unsupported fixture action'}},404);
  });
  const load=async()=>{await page.goto('http://127.0.0.1:8854/module-fixture');await page.getByRole('heading',{name:'Operational modules',exact:true}).waitFor()};
  await load();check(requests.every(r=>r.path.endsWith('/status')||r.path==='/api/session'),'mount is read only');
  check(await page.getByText('Running',{exact:true}).isVisible()&&await page.getByText('Held',{exact:true}).isVisible(),'running supervision differs from entry hold');
  check(await page.getByText('Saved module drafts below do not install or change them.',{exact:false}).isVisible(),'operational and draft modules distinguished');
  check(await page.getByRole('button',{name:'Enable demo entries',exact:true}).isDisabled(),'fresh module does not enable trading');
  await page.evaluate(()=>window.fixtureClock+=13000);await page.getByText('Expired',{exact:true}).waitFor();
  check(await page.getByText('Not confirmed',{exact:true}).isVisible(),'fresh proof ages out without response');
  await page.getByText('Module settings and history binding',{exact:true}).click();
  await page.getByLabel('Maximum entry evidence age, seconds',{exact:true}).fill('13');
  check(await page.getByRole('button',{name:'Save module settings',exact:true}).isDisabled(),'cannot loosen beyond12 seconds');
  await page.getByLabel('Maximum entry evidence age, seconds',{exact:true}).fill('2.5');
  await page.getByRole('button',{name:'Save module settings',exact:true}).click();await page.getByText('Module settings saved.',{exact:false}).waitFor();
  check(requests.some(r=>r.path.endsWith('/configure-module')&&r.body.expectedRevision===1&&r.body.configuration.entryMaxAgeMs===2500),'configuration exact revision and seconds conversion');
  check(await page.getByText('Waiting for a new account reading after this change.',{exact:true}).isVisible(),'new settings cannot preserve fresh claim');
  await page.getByRole('button',{name:'Review module retirement',exact:true}).click();
  check(!requests.some(r=>r.path.endsWith('/retire-module')),'review is not mutation');
  refuseModule=true;await page.getByRole('button',{name:'Confirm module retirement',exact:true}).click();await page.getByRole('alert').filter({hasText:'still needs Guardian supervision'}).waitFor();
  check(module.lifecycleState==='installed'&&!await page.getByText('Guardian module retired;',{exact:false}).count(),'refused retirement has no success claim');
  refuseModule=false;await page.getByRole('button',{name:'Refresh runtime status',exact:true}).click();await page.getByRole('button',{name:'Review module retirement',exact:true}).click();await page.getByRole('button',{name:'Confirm module retirement',exact:true}).click();await page.getByText('Guardian module retired;',{exact:false}).waitFor();
  check(module.lifecycleState==='retired'&&instance.entriesHeld,'retirement keeps entries held');
  await page.getByRole('button',{name:'Review module installation',exact:true}).click();await page.getByRole('button',{name:'Confirm module installation',exact:true}).click();await page.getByText('Guardian module installed with entries held.',{exact:false}).waitFor();
  check(module.lifecycleState==='installed'&&module.observationState==='unknown'&&requests.some(r=>r.path.endsWith('/install-module')&&r.body.expectedRevision===3),'reattach held with unknown proof and exact revision');
  await page.getByText('Private alert delivery',{exact:true}).click();await page.getByRole('button',{name:'Show saved private address',exact:true}).click();await page.getByLabel('Private alert address',{exact:true}).waitFor();
  check(await page.getByLabel('Private alert address',{exact:true}).getAttribute('type')==='password','explicit private address masked');
  check(!await page.locator('body').innerText().then(value=>value.includes('FAKE-GENERATION')),'private address not normal text');
  await page.getByRole('button',{name:'Review key replacement',exact:true}).click();check(!requests.some(r=>r.path.endsWith('/rotate-signal-key')),'rotation review sends nothing');
  await page.getByRole('button',{name:'Confirm key replacement',exact:true}).click();await page.getByText('Alert key replaced.',{exact:false}).waitFor();
  check(state.ingressKeys.producers[0].generation===2&&requests.find(r=>r.path.endsWith('/rotate-signal-key')).body.expectedGeneration===1,'explicit rotation bound to current generation');
  check((await page.getByLabel('Private alert address',{exact:true}).inputValue()).endsWith('FAKE-GENERATION-2'),'new response only contains current address');
  await page.getByRole('button',{name:'Dismiss private address',exact:true}).click();await page.getByRole('button',{name:'Review key replacement',exact:true}).click();loseRotation=true;await page.getByRole('button',{name:'Confirm key replacement',exact:true}).click();await page.getByRole('alert').waitFor();
  check(state.ingressKeys.producers[0].generation===3&&requests.filter(r=>r.path.endsWith('/rotate-signal-key')).length===2,'unknown response is not automatically replayed');
  await page.getByRole('button',{name:'Refresh runtime status',exact:true}).click();await page.getByText('Private alert delivery',{exact:true}).click();await page.getByRole('button',{name:'Show saved private address',exact:true}).click();await page.getByLabel('Private alert address',{exact:true}).waitFor();
  check((await page.getByLabel('Private alert address',{exact:true}).inputValue()).endsWith('FAKE-GENERATION-3'),'lost response recovered by reading current saved address');
  check(requests.filter(r=>r.path.endsWith('/rotate-signal-key')).length===2,'recovery does not rotate again');
  await page.getByRole('button',{name:'Dismiss private address',exact:true}).click();await page.getByRole('button',{name:'Review key replacement',exact:true}).click();state.ingressKeys.producers[0].generation=4;
  await page.getByText('The saved generation changed.',{exact:false}).waitFor();check(await page.getByRole('button',{name:'Confirm key replacement',exact:true}).isDisabled(),'pending confirmation rejects changed generation');
  instance.tradePhase='in_trade';await load();check(await page.getByRole('button',{name:'Review module retirement',exact:true}).isDisabled(),'active trade cannot retire module in UI');
  await page.getByText('Private alert delivery',{exact:true}).click();check(await page.getByRole('button',{name:'Review key replacement',exact:true}).isDisabled(),'active trade cannot rotate key in UI');
  instance.tradePhase='closed';state.routes.locked=true;state.ingressKeys.locked=true;await load();await page.getByText('Private alert delivery',{exact:true}).click();check(await page.getByRole('button',{name:'Show saved private address',exact:true}).isDisabled(),'locked vault cannot reveal secret');
  state.routes.locked=false;state.ingressKeys.locked=false;module.observationState='unknown';module.runtimeState='failed';await load();check(await page.getByText('Needs attention',{exact:true}).isVisible(),'failed observer is not running');
  await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
  for(const width of [390,320]){await page.setViewportSize({width,height:1100});check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),width+'px no horizontal overflow')}
  await page.screenshot({path:path.join(out,'narrow.png'),fullPage:true});
  module.sourceCurrent=false;await load();check(await page.getByText('Update review needed',{exact:true}).isVisible(),'changed implementation cannot claim current running module');check(!await page.getByRole('button',{name:'Review module installation',exact:true}).isDisabled(),'held current-source reinstallation remains reachable');
  module.sourceCurrent=true;instance.binding.engineSha256='9'.repeat(64);await load();check(await page.getByRole('button',{name:'Review module retirement',exact:true}).isDisabled(),'wrong exact engine pair disables lifecycle action');instance.binding.engineSha256=engineSha256;
  delete state.operationalModules;await load();check(await page.getByText('Module status is unavailable.',{exact:false}).isVisible(),'missing status is unknown not uninstalled');
  check(await page.getByRole('button',{name:'Review module installation',exact:true}).count()===0,'unknown module status cannot install');
  check(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0),'no browser secret storage');check(errors.length===0,'no browser exception');
  fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({passed:count,scope:'actual runtime component, local API fixtures; no native provider/actions',requests:requests.map(r=>({path:r.path,fields:Object.keys(r.body||{})}))},null,2)+'\n');console.log(JSON.stringify({passed:count,evidence:out}));
 }finally{await browser.close();await server.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
