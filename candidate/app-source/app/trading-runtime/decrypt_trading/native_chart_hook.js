(() => {
  // Separate hook/binding from the existing NQ observation reader.
  const chartUrl = __CUSTOMER_CHART_URL__;
  const binding = __CUSTOMER_BINDING__;
  const queue = [];
  let overflow = false;
  window[binding] = raw => { if(queue.length >= 128) overflow = true; else queue.push(JSON.parse(raw)); };
  const key = __CUSTOMER_READER_KEY__;
  if (window[key]) window[key].stop();
  if (location.href !== chartUrl) throw new Error('wrong_chart');
  const collection = window._exposed_chartWidgetCollection;
  const widget = collection?.activeChartWidget?.value();
  const series = widget?.model()?.mainSeries();
  if (!series) throw new Error('chart_model_missing');
  const state = {stopped:false, counter:0, native5:null, ownSession:null,
    fiveResolved:null, fiveMode:null, fiveFailed:false, fiveRestarts:0, fiveStarted:performance.now(), leaseUntil:performance.now()+5000};
  function health() {
    const info = series.symbolInfo();
    return {
      page_url:location.href,
      same_series:collection.activeChartWidget.value()===widget && widget.model().mainSeries()===series,
      series_symbol:widget.symbol().value(), timeframe:widget.interval().value(),
      chart_style:series.style(), chart_ready:widget.isReady(), chart_failed:widget.isFailed(), series_status:series.status(),
      is_delayed:series.dataUpdatedModeModel().isDelay(), replay_status:widget.model().replayStatus().value(),
      symbol_info:{full_name:info?.full_name,minmov:info?.minmov,pricescale:info?.pricescale},
      stopped:state.stopped, counter:state.counter,
      native5_status:{active:state.native5?.isActive()===true,
        connected:state.ownSession?.isConnected().value()===true,
        resolution:state.native5?.resolution(),symbol_info:state.fiveResolved,
        update_mode:state.fiveMode,failed:state.fiveFailed,
        last_bar_open_ms:state.native5?.data?.()?.bars()?.last()?.value?.[0]*1000 || null,
        reconnects:state.fiveRestarts}
    };
  }
  state.health=()=>{
    state.leaseUntil=performance.now()+5000;
    // Renew only our silent auxiliary subscription. Never change the visible chart,
    // replay historical candles, or reconnect merely because the market is closed.
    const now=Date.now(), mainAt=series.bars().last()?.value?.[0]*1000;
    const fiveAt=state.native5?.data?.()?.bars()?.last()?.value?.[0]*1000;
    if(!state.stopped && now-mainAt>=0 && now-mainAt<=5000 &&
       (!Number.isFinite(fiveAt) || now-fiveAt>30000) && performance.now()-state.fiveStarted>=60000) {
      startFive(); state.fiveRestarts++;
    }
    return health();
  };
  state.callback=(_metadata,history)=>{
    if(state.stopped)return;
    try {
      const bars=series.bars(), last=bars.last();
      const rows=[];
      if(last) {
        // Native model candles, not OHLC reconstructed from price polling.
        // Last row remains forming. Python requires a later native bar before closure.
        for(let i=Math.max(bars.firstIndex(),last.index-31);i<=last.index;i++) {
          const value=bars.valueAt(i);
          if(value)rows.push([...value]);
        }
      }
      window[binding](JSON.stringify({...health(),event_kind:'data_updated',
        history,callback_at:new Date().toISOString(),bar:last?[...last.value]:null,
        rows,hook_counter:++state.counter}));
    } catch (_) {window[binding](JSON.stringify({event_kind:'reader_error'}));}
  };
  state.stop=()=>{
    if(state.stopped)return;
    state.stopped=true;
    clearInterval(state.leaseTimer);
    series.dataEvents().dataUpdated().unsubscribe(state,state.callback);
    state.native5?.destroy();
    state.ownSession?.destroy();
  };
  state.fiveCallback=(_metadata,history)=>{
    if(state.stopped)return;
    try {
      const bars=state.native5.data().bars(),last=bars.last(),rows=[];
      if(last)for(let i=Math.max(bars.firstIndex(),last.index-7);i<=last.index;i++) {
        const value=bars.valueAt(i);if(value)rows.push([...value]);
      }
      window[binding](JSON.stringify({...health(),event_kind:'native_five_updated',history,
        callback_at:new Date().toISOString(),bar:last?[...last.value]:null,rows,
        hook_counter:++state.counter}));
    }catch(_){window[binding](JSON.stringify({event_kind:'reader_error'}));}
  };
  // The server permits one primary series per ChartSession. Never create this
  // secondary series inside the visible chart's own session.
  function startFive() {
   state.native5?.destroy(); state.ownSession?.destroy();
   state.native5=null; state.ownSession=null;
   state.fiveResolved=null; state.fiveMode=null; state.fiveFailed=false; state.fiveStarted=performance.now();
   try {
    const mainSource=series.seriesSource(),mainSession=widget.chartSession();
    state.ownSession=new mainSession.constructor(mainSession.chartApi(),mainSession.wireFormat());
    state.ownSession.criticalError().subscribe(state,()=>{state.fiveFailed=true;});
    state.ownSession.connect();
    state.native5=new mainSource.constructor(state.ownSession,
      'decrypt_v3_'+crypto.randomUUID().replaceAll('-','_'),{count:32});
    const de=state.native5.dataEvents();
    de.symbolResolved().subscribe(state,info=>{
      state.fiveResolved={full_name:info.full_name,minmov:info.minmov,pricescale:info.pricescale};
    });
    de.completed().subscribe(state,info=>{state.fiveMode=info.updateMode;});
    de.seriesError().subscribe(state,()=>{state.fiveFailed=true;});
    de.symbolError().subscribe(state,()=>{state.fiveFailed=true;});
    de.dataUpdated().subscribe(state,state.fiveCallback);
    state.native5.modifySeries(JSON.parse(JSON.stringify(mainSource.symbol())),'5S');
    state.native5.start();
   }catch(error){state.native5?.destroy();state.ownSession?.destroy();throw error;}
  }
  startFive();
  series.dataEvents().dataUpdated().subscribe(state,state.callback);
  state.leaseTimer=setInterval(()=>{
    if(performance.now()>state.leaseUntil)state.stop();
  },1000);
  state.drain=()=>({health:state.health(),overflow,packets:queue.splice(0)});
  window[key]=state;
  // Never emit an initial snapshot or historical batch as a live quote.
  return health();
})()
