// Actual React component against explicit local API fixtures, no native actions.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {pathToFileURL}=require('node:url');
const root=path.resolve(__dirname,'..'),ui=path.join(root,'app/ui');
const evidence=process.env.CARE_UI_EVIDENCE||path.resolve(root,'../../evidence/system-care-ui');
const playwright=require(process.env.PLAYWRIGHT_MODULE);
const backup='a'.repeat(32),restore='b'.repeat(32),transaction='c'.repeat(32);
const now=Date.now();let count=0;
const check=(condition,message)=>{assert.ok(condition,message);count++;};

(async()=>{
 const {createServer}=await import(pathToFileURL(path.join(ui,'node_modules/vite/dist/node/index.js')).href);
 let server;server=await createServer({root:ui,configFile:path.join(ui,'vite.config.ts'),
  resolve:{alias:{react:path.join(ui,'node_modules/react'),'react-dom':path.join(ui,'node_modules/react-dom')}},
  server:{host:'127.0.0.1',port:8853,strictPort:true,fs:{allow:[root]}},
  plugins:[{name:'care-test-entry',configureServer(s){s.middlewares.use('/system-care-fixture',async(req,res,next)=>{
   try{const html=await s.transformIndexHtml('/system-care-fixture','<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main id="root" style="max-width:1160px;margin:0 auto;padding:24px"></main><script type="module" src="/@fs'+path.join(root,'tests/system_care_entry.tsx')+'"></script></body></html>');res.setHeader('Content-Type','text/html');res.end(html);}catch(e){next(e);}
  });}}]});
 await server.listen();
 const browser=await playwright.chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try{
  fs.mkdirSync(evidence,{recursive:true});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});const consoleErrors=[];
  page.on('pageerror',e=>consoleErrors.push(String(e)));
  let trading={enabled:true,instances:[{instanceId:'fixture-engine',binding:{symbol:'MNQZ2026'},calendar:{state:'hold',reason:'calendar_outside_reviewed_horizon',entryAllowed:false,expectedSymbol:'MNQH2027',coverageEnds:'2026-12-31',managementAllowed:true,checkedAt:now,validUntil:now+60000}}],notifications:{enabled:false,pending:0,events:[],userRead:false,receiptSemantics:'desktop_daemon_acceptance_not_user_read'}};
  let maintenance={schema:'decrypt.maintenance-status.v1',backupCount:1,backups:[{backupId:backup,bytes:65536}],restores:[],transactions:[{transactionId:transaction,phase:'recovery_required'}],stagedVersions:['beta-reviewed'],currentVersion:'installed',nativeReconciliationRequired:false,restartRequired:false,largeArchiveTransfer:'local_cli',installedBaselineConfigured:true,restoredTradingReconciliationImplemented:true,reconciliation:{state:'not_required',available:false,proofsCollected:0}};
  const requests=[];let failMaintenance=false,failTrading=false,refuseApply=false,holdBackup=null;
  await page.route('**/api/**',async route=>{
   const req=route.request(),url=new URL(req.url()),body=req.method()==='POST'?req.postDataJSON():null;
   requests.push({path:url.pathname,method:req.method(),body});
   const respond=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
   if(url.pathname==='/api/session')return respond({csrfToken:'fixture-only-token'});
   if(url.pathname==='/api/trading/status')return failTrading?respond({error:{code:'fixture',message:'unavailable'}},503):respond(trading);
   if(url.pathname==='/api/trading/configure-notifications'){trading.notifications.enabled=body.enabled;return respond(trading);}
   if(url.pathname==='/api/maintenance')return failMaintenance?respond({error:{code:'fixture',message:'unavailable'}},503):respond({maintenance});
   if(url.pathname==='/api/maintenance/backup'){
    if(holdBackup)await holdBackup;
    return respond({maintenance,result:{backupId:backup,credentialsIncluded:body.includeCredentials}});
   }
   if(url.pathname==='/api/maintenance/restore/prepare'){
    maintenance.restores=[{restoreId:restore,fileCount:3,credentialsIncluded:false,status:'staged'}];
    return respond({maintenance,result:{restoreId:restore,status:'staged',fileCount:3}});
   }
   if(url.pathname==='/api/maintenance/restore/apply'){
    if(refuseApply)return respond({error:{code:'maintenance_active_trade',message:'fixture backend refusal'}},409);
    maintenance.nativeReconciliationRequired=true;maintenance.restartRequired=true;maintenance.reconciliation={state:'held',available:true,proofsCollected:0};
    return respond({maintenance,result:{status:'restored_held'}});
   }
   if(url.pathname==='/api/maintenance/restore/reconcile'){
    if(maintenance.reconciliation.proofsCollected===0){maintenance.reconciliation={state:'awaiting_second_observation',available:true,proofsCollected:1};return respond({maintenance,result:{status:'awaiting_second_observation',proofsCollected:1}});}
    maintenance.nativeReconciliationRequired=false;maintenance.restartRequired=true;maintenance.reconciliation={state:'reconciled',available:false,proofsCollected:2};return respond({maintenance,result:{status:'reconciled_held'}});
   }
   if(url.pathname==='/api/maintenance/release/activate'){maintenance.currentVersion=body.version;maintenance.restartRequired=true;return respond({maintenance,result:{status:'activated_for_restart'}});}
   if(url.pathname==='/api/maintenance/release/rollback'){maintenance.currentVersion='installed';return respond({maintenance,result:{status:'rolled_back_for_restart'}});}
   if(url.pathname==='/api/maintenance/restore/recover'){maintenance.transactions=[];return respond({maintenance,result:{status:'rolled_back_held'}});}
   if(url.pathname==='/api/maintenance/offboard')return respond({maintenance,result:{status:'local_credentials_removed_held'}});
   return respond({error:{code:'not_found',message:'Fixture has no such route'}},404);
  });
  await page.goto('http://127.0.0.1:8853/system-care-fixture');
  await page.getByRole('button',{name:'Enable desktop alerts',exact:true}).waitFor();
  check(requests.filter(r=>r.path!=='/api/session').every(r=>r.path.endsWith('/status')||r.path==='/api/maintenance'),'mount must not mutate');
  check(await page.getByText('Installed baseline',{exact:true}).isVisible(),'installed sentinel labels the configured baseline');
  check(await page.getByText('The date is outside the reviewed calendar.',{exact:false}).isVisible(),'horizon hold shown');
  await page.getByRole('button',{name:'Enable desktop alerts',exact:true}).click();
  await page.getByRole('button',{name:'Disable desktop alerts',exact:true}).waitFor();
  check(requests.some(r=>r.path==='/api/trading/configure-notifications'&&r.body.enabled===true),'explicit opt in');
  trading.notifications.events=[{id:1,source:'guardian',kind:'outage',created:now,status:'accepted',attempts:1,next_attempt:0,last_outcome:'accepted'},{id:2,source:'market:fixture',kind:'outage',created:now,status:'retry',attempts:2,next_attempt:now+60000,last_outcome:'failed'}];
  await page.getByRole('button',{name:'Refresh system care'}).click();await page.getByText('Delivery receipts · 2').click();
  check(await page.getByText('Accepted by desktop · read status unknown').isVisible(),'desktop accepted not read');
  check(await page.getByText('Delivery failed · retry scheduled').isVisible(),'retry receipt shown');
  await page.getByText('Create encrypted backup',{exact:true}).first().click();
  await page.getByLabel('New archive passphrase',{exact:true}).fill('FIXTURE-ONLY-SECRET');
  await page.getByLabel('Confirm archive passphrase',{exact:true}).fill('DIFFERENT-FIXTURE');
  await page.getByRole('button',{name:'Create encrypted backup',exact:true}).click();
  check(await page.getByText('The passphrases did not match.').isVisible(),'mismatch before request');
  check(!requests.some(r=>r.path==='/api/maintenance/backup'),'mismatch sends no secret');
  check(await page.getByLabel('New archive passphrase',{exact:true}).inputValue()==='','mismatch clears secret');
  let release;holdBackup=new Promise(resolve=>release=resolve);
  await page.getByLabel('New archive passphrase',{exact:true}).fill('FIXTURE-ONLY-SECRET');
  await page.getByLabel('Confirm archive passphrase',{exact:true}).fill('FIXTURE-ONLY-SECRET');
  await page.getByRole('button',{name:'Create encrypted backup',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#care-backup-passphrase').value==='');
  check(await page.getByLabel('New archive passphrase',{exact:true}).getAttribute('type')==='password','secret masked');
  check(await page.getByRole('button',{name:'Create encrypted backup',exact:true}).isDisabled(),'inflight disabled');
  release();holdBackup=null;await page.getByText('Encrypted archive created locally.',{exact:false}).waitFor();
  check(requests.filter(r=>r.path==='/api/maintenance/backup').length===1,'single backup request');
  check(requests.find(r=>r.path==='/api/maintenance/backup').body.includeCredentials===false,'credentials opt in defaults false');
  check(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0),'no browser storage');
  await page.getByText('Restore saved records · 1 local archives').click();
  await page.getByLabel('Saved archive',{exact:true}).selectOption(backup);
  await page.getByLabel('Restore archive passphrase',{exact:true}).fill('FIXTURE-ONLY-SECRET');
  await page.getByRole('button',{name:'Prepare restore',exact:true}).click();
  await page.getByLabel('Verified staged restore').waitFor();
  check(await page.getByLabel('Restore archive passphrase',{exact:true}).inputValue()==='','prepare clears passphrase');
  await page.getByLabel('Verified staged restore').selectOption(restore);
  check(await page.getByRole('button',{name:'Apply staged restore'}).isDisabled(),'explicit apply acknowledgment');
  await page.getByLabel('I want to apply these staged records',{exact:false}).check();refuseApply=true;
  await page.getByRole('button',{name:'Apply staged restore'}).click();
  await page.getByText('An active trade still needs supervision.',{exact:false}).waitFor();
  check(!await page.getByText('Restore applied.',{exact:false}).count(),'refusal no success claim');
  refuseApply=false;await page.getByRole('button',{name:'Refresh system care'}).click();
  await page.getByText('Restore saved records · 1 local archives').click();
  await page.getByLabel('Verified staged restore').selectOption(restore);await page.getByLabel('I want to apply these staged records',{exact:false}).check();
  await page.getByRole('button',{name:'Apply staged restore'}).click();
  await page.getByText('Restore applied.',{exact:false}).waitFor();
  check(await page.getByText('A restart alone does not clear this hold.',{exact:false}).isVisible(),'hold persists');
  check(await page.getByRole('button',{name:'Check restored records',exact:true}).isDisabled(),'reconcile disabled before restart');
  maintenance.restartRequired=false;await page.getByRole('button',{name:'Refresh system care'}).click();
  await page.getByLabel('Check these restored records without enabling entries or replaying orders.',{exact:true}).check();
  await page.getByRole('button',{name:'Check restored records',exact:true}).click();await page.getByText('First broker reading retained.',{exact:false}).waitFor();
  check(maintenance.nativeReconciliationRequired,'first reading does not clear hold');
  check(requests.filter(r=>r.path==='/api/maintenance/restore/reconcile').length===1,'no automatic second request');
  await page.getByLabel('Check these restored records without enabling entries or replaying orders.',{exact:true}).check();
  await page.getByRole('button',{name:'Check second broker reading',exact:true}).click();await page.getByText('Restored records verified.',{exact:false}).waitFor();
  check(requests.filter(r=>r.path==='/api/maintenance/restore/reconcile').every(r=>Object.keys(r.body).length===0),'browser submits no native proof or arbitrary identity');
  check(await page.getByText('This did not enable entries or replay orders.',{exact:false}).isVisible(),'reconciliation remains held');
  maintenance.nativeReconciliationRequired=true;maintenance.reconciliation={state:'held',available:false,proofsCollected:0};await page.getByRole('button',{name:'Refresh system care'}).click();
  await page.getByText('This hold is not available for automatic reconciliation.',{exact:false}).waitFor();
  check(!await page.getByRole('button',{name:'Check restored records',exact:true}).count(),'unsupported hold has no reconciliation action');
  await page.getByText('Update or roll back the app',{exact:true}).click();
  await page.getByLabel('Verified staged release').selectOption('beta-reviewed');await page.getByRole('button',{name:'Review activation'}).click();
  check(!requests.some(r=>r.path==='/api/maintenance/release/activate'),'review is not activation');
  await page.getByRole('button',{name:'Activate verified release'}).click();await page.getByText('Verified release selected for restart.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Review rollback'}).click();await page.getByRole('button',{name:'Roll back verified release'}).click();await page.getByText('Previous verified release selected',{exact:false}).waitFor();
  check(requests.some(r=>r.path==='/api/maintenance/release/rollback'&&Object.keys(r.body).length===0),'rollback no arbitrary path');
  await page.getByText('Recover an interrupted restore',{exact:true}).click();await page.getByLabel('Interrupted transaction').selectOption(transaction);await page.getByLabel('Recover this recorded transaction',{exact:false}).check();await page.getByRole('button',{name:'Recover interrupted restore',exact:true}).click();await page.getByText('Interrupted restore recovery completed.',{exact:false}).waitFor();
  check(requests.some(r=>r.path==='/api/maintenance/restore/recover'&&r.body.transactionId===transaction),'known transaction');
  await page.getByText('Transfer archives or signed updates',{exact:true}).click();
  check(await page.getByText('/usr/local/lib/decrypt-os-runtime/python/bin/python',{exact:false}).isVisible(),'real pinned local helper');
  check(await page.locator('input[type=file]').count()===0,'no oversized upload or arbitrary file API');
  await page.getByText('Remove saved local sign-ins',{exact:true}).click();
  check(await page.getByRole('button',{name:'Remove local sign-ins',exact:true}).isDisabled(),'offboard exact confirmation');
  await page.getByLabel('Type REMOVE SAVED SIGN-INS to confirm').fill('REMOVE SAVED SIGN-INS');await page.getByRole('button',{name:'Remove local sign-ins',exact:true}).click();await page.getByText('Saved local credentials removed.',{exact:false}).waitFor();
  check(await page.getByText('Browser sign-out and provider revocation still require separate completion.',{exact:false}).isVisible(),'offboard remaining work explicit');
  check(await page.getByLabel('Type REMOVE SAVED SIGN-INS to confirm').inputValue()==='','confirmation cleared');
  await page.screenshot({path:path.join(evidence,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(evidence,'mobile.png'),fullPage:true});
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile no horizontal overflow');
  await page.getByRole('button',{name:'Refresh system care'}).focus();await page.keyboard.press('Tab');
  check(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle!=='none'),'keyboard focus visible');
  trading.instances[0].calendar={...trading.instances[0].calendar,state:'open',reason:'reviewed_regular_session',entryAllowed:true,checkedAt:Date.now(),validUntil:Date.now()+1800};
  await page.getByRole('button',{name:'Refresh system care'}).click();
  await page.getByText('Calendar permits new entries; strategy, broker and feed checks still apply.').waitFor();
  check(await page.getByText('Calendar permits new entries; strategy, broker and feed checks still apply.').isVisible(),'calendar open is only one condition');
  await page.getByText('This calendar reading has expired.',{exact:false}).waitFor();
  check(!await page.getByText('Calendar permits new entries; strategy, broker and feed checks still apply.').count(),'stale open expires without next API response');
  failMaintenance=true;await page.getByRole('button',{name:'Refresh system care'}).click();await page.getByText('Backup and maintenance status are unavailable.',{exact:false}).waitFor();
  check(await page.getByRole('button',{name:'Disable desktop alerts',exact:true}).isVisible(),'independent health still available');
  failTrading=true;await page.getByRole('button',{name:'Refresh system care'}).click();await page.getByText('Health and calendar status are unavailable.',{exact:false}).waitFor();
  check(!await page.getByText('The date is outside the reviewed calendar.',{exact:false}).count(),'failed poll discards old calendar');
  check(consoleErrors.length===0,'no React/runtime errors: '+consoleErrors.join('\n'));
  fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify({checks:count,passed:true,scope:'actual component; local API fixtures; no native maintenance or notification',requests:requests.map(r=>({path:r.path,method:r.method,fields:r.body?Object.keys(r.body):[]}))},null,2)+'\n');
  console.log(JSON.stringify({checks:count,passed:true,evidence}));
 }finally{await browser.close();await server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
