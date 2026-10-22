// Execute the actual customer hook with an isolated native-chart API fixture.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const root=path.resolve(__dirname,'../app/trading-runtime/decrypt_trading');
let src=fs.readFileSync(path.join(root,'native_chart_hook.js'),'utf8');
const url='https://www.tradingview.com/chart/Test1234/';
for(const [key,value] of Object.entries({__CUSTOMER_CHART_URL__:url,__CUSTOMER_BINDING__:'testDeliver',__CUSTOMER_READER_KEY__:'testReader'}))src=src.replaceAll(key,JSON.stringify(value));
class Bus{constructor(){this.handlers=[];}subscribe(owner,fn){this.handlers.push([owner,fn]);}unsubscribe(owner,fn){this.handlers=this.handlers.filter(x=>x[0]!==owner||x[1]!==fn);}emit(...args){for(const [o,f] of this.handlers)f.apply(o,args);}}
const types=['symbolResolved','completed','seriesError','symbolError','dataUpdated'];
const events=()=>Object.fromEntries(types.map(k=>[k,new Bus()]));
const publicEvents=object=>Object.fromEntries(Object.entries(object).map(([k,v])=>[k,()=>v]));
const rows=[[1631196004,24000,24000.25,23999.75,24000,1],[1631196005,24000,24000.5,24000,24000.25,2]];
const bars={firstIndex:()=>0,last:()=>({index:rows.length-1,value:rows.at(-1)}),valueAt:i=>rows[i]};
let primaryConnects=0,mainChanged=0,native=null,auxSessions=0;
const chartApi={},wireFormat={encodeSymbol:symbol=>symbol};
class Session{constructor(api,format){assert.strictEqual(api,chartApi);assert.strictEqual(format,wireFormat);auxSessions++;this.errors=new Bus();}chartApi(){return {};}criticalError(){return this.errors;}connect(){this.connected=true;}isConnected(){return{value:()=>!!this.connected};}destroy(){this.connected=false;}}
class Series{constructor(session){this.session=session;this.ev=events();native=this;}symbol(){return{symbol:'CME_MINI:MNQZ2026'};}dataEvents(){return publicEvents(this.ev);}modifySeries(symbol,res){this.symbolValue=symbol;this.res=res;}resolution(){return this.res;}start(){this.active=true;}isActive(){return this.active;}destroy(){this.active=false;}data(){return{bars:()=>bars};}}
const mainSource={constructor:Series,symbol:()=>({symbol:'CME_MINI:MNQZ2026'})}, mainEvents=events();
const main={symbolInfo:()=>({full_name:'CME_MINI:MNQZ2026',minmov:25,pricescale:100}),style:()=>1,status:()=>3,dataUpdatedModeModel:()=>({isDelay:()=>false}),bars:()=>bars,seriesSource:()=>mainSource,dataEvents:()=>publicEvents(mainEvents)};
const primary={constructor:Session,chartApi:()=>chartApi,wireFormat:()=>wireFormat,connect:()=>primaryConnects++};
const model={mainSeries:()=>main,replayStatus:()=>({value:()=>0})};
const widget={model:()=>model,symbol:()=>({value:()=> 'CME_MINI:MNQZ2026'}),interval:()=>({value:()=> '1S'}),isReady:()=>true,isFailed:()=>false,chartSession:()=>primary};
const window={_exposed_chartWidgetCollection:{activeChartWidget:{value:()=>widget}}};
let mono=0,interval;
const context={window,location:{href:url},performance:{now:()=>mono},Date,crypto:{randomUUID:()=> 'fixture-uuid'},setInterval:fn=>(interval=fn,1),clearInterval:()=>{}};
vm.createContext(context);const health=vm.runInContext(src,context);
assert.equal(health.same_series,true);assert.equal(health.series_symbol,'CME_MINI:MNQZ2026');
assert.equal(primaryConnects,0);assert.equal(auxSessions,1);assert.equal(native.resolution(),'5S');
assert.equal(window.testReader.drain().packets.length,0,'installation must not publish historical/current snapshot');
mainEvents.dataUpdated.emit({},false);
let result=window.testReader.drain();assert.equal(result.packets.length,1);assert.equal(result.packets[0].event_kind,'data_updated');assert.equal(result.packets[0].rows.length,2);assert.equal(result.packets[0].history,false);
native.ev.symbolResolved.emit({full_name:'CME_MINI:MNQZ2026',minmov:25,pricescale:100});native.ev.completed.emit({updateMode:'streaming'});native.ev.dataUpdated.emit({},false);
result=window.testReader.drain();assert.equal(result.packets[0].event_kind,'native_five_updated');assert.equal(result.packets[0].native5_status.update_mode,'streaming');
for(let i=0;i<129;i++)mainEvents.dataUpdated.emit({},false);
result=window.testReader.drain();assert.equal(result.overflow,true);assert.equal(result.packets.length,128);
mono=6000;interval();assert.equal(window.testReader.stopped,true);assert.equal(native.isActive(),false);assert.equal(mainEvents.dataUpdated.handlers.length,0);assert.equal(context.location.href,url);
console.log(JSON.stringify({passed:true,scope:'actual hook in isolated native chart API fixture; no real provider'}));
