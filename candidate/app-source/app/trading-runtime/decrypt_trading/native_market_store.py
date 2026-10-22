"""Native chart quote/bar adapter for the unchanged Soup v3 channel contract."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3

from .native_chart_contract import validate_chart_event

SOURCE='tradingview_native_ohlcv;quote=main_series;bar5=independent_chart_session_same_api;quote_time=browser_callback_ms;not_exchange_timestamp'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def milliseconds(iso):
    return int(datetime.fromisoformat(iso.replace('Z','+00:00')).timestamp()*1000)


def native_row(row):
    if not isinstance(row,list) or len(row)!=6:
        raise ValueError('native_row_shape')
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in row):
        raise ValueError('native_row_number')
    stamp,o,h,l,c,v=row
    if stamp<=0 or int(stamp)!=stamp or min(o,h,l,c)<=0 or v<0:
        raise ValueError('native_row_range')
    if not l<=min(o,c)<=max(o,c)<=h:
        raise ValueError('native_row_ohlc')
    if any(Decimal(str(x))%Decimal('.25') for x in (o,h,l,c)):
        raise ValueError('native_row_tick')
    return int(stamp)*1000,(float(o),float(h),float(l),float(c))


def bars_from_event(packet,now_ms,interval=1000,*,symbol):
    """Finality requires next native candle. Gaps never become invented candles."""
    rows=packet.get('rows')
    if not isinstance(rows,list) or not 1<=len(rows)<=32:
        raise ValueError('native_rows_missing_or_unbounded')
    parsed=[native_row(row) for row in rows]
    if any(a[0]>=b[0] for a,b in zip(parsed,parsed[1:])):
        raise ValueError('native_rows_not_ordered')
    if rows[-1]!=packet.get('bar'):
        raise ValueError('native_last_row_mismatch')
    if interval not in (1000,5000):raise ValueError('unsupported_bar_interval')
    if any(a%interval for a,p in parsed):raise ValueError('native_bar_alignment')
    current=parsed[-1][0]
    complete={a:p for a,p in parsed[:-1] if a+interval<=current and 0<=now_ms-(a+interval)<=15000}
    def pack(a,span,ohlc):
        return {'event_id':f'tvbar:{symbol}:{span//1000}:{a}', 'symbol':symbol,
                'open_time':a,'close_time':a+span,**dict(zip(('open','high','low','close'),ohlc))}
    result=[pack(a,interval,p) for a,p in complete.items()]
    return sorted(result,key=lambda x:(x['close_time'],x['close_time']-x['open_time']))


class MarketStore:
    def __init__(self,path,*,symbol,chart_url,retention=100000):
        self.symbol,self.chart_url=symbol,chart_url
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.retention=max(1000,int(retention))
        self.db=sqlite3.connect(path,timeout=5,check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,
            source TEXT NOT NULL,observed_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS cursor(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS finalized(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,close_time INTEGER NOT NULL);
        ''')
        self.path.chmod(0o600)
        row=self.db.execute("SELECT value FROM cursor WHERE key='previous'").fetchone()
        self.previous=json.loads(row[0]) if row else None
        self.sequence=int(self.db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0])
        self.changed_candle=False

    def ingest(self,packet,now_ms):
        if self.changed_candle:raise ValueError('finalized_native_candle_changed_restart_review_required')
        if packet.get('same_series') is not True or packet.get('stopped') is not False:
            raise ValueError('chart_identity_changed')
        if type(packet.get('chart_style')) is not int or packet['chart_style']!=1:
            raise ValueError('not_standard_time_chart')
        if packet.get('event_kind')=='native_five_updated':
            return self.ingest_five(packet,now_ms)
        now=datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat()
        prev=self.previous
        event=validate_chart_event(packet,sequence=(prev['sequence']+1 if prev else 1),now=now,chart_url=self.chart_url,symbol=self.symbol,
                                   previous=prev,max_age=2)
        if event['status']!='fresh':
            if event['reasons']==['duplicate_chart_state']:return {'quote':0,'bar1':0,'bar5':0,'reason':'duplicate_chart_state'}
            raise ValueError('chart_validation:'+','.join(event['reasons']))
        at=milliseconds(event['observed_at'])
        if prev and at<=milliseconds(prev['observed_at']):
            return {'quote':0,'bar1':0,'bar5':0,'reason':'callback_ms_not_advancing'}
        bars=bars_from_event(packet,now_ms,symbol=self.symbol)
        # Reject corrections rather than silently send different OHLC for same finished bar.
        for bar in bars:
            old=self.db.execute('SELECT payload FROM finalized WHERE event_id=?',(bar['event_id'],)).fetchone()
            if old and old[0]!=canonical(bar):
                self.changed_candle=True
                raise ValueError('finalized_native_candle_changed_restart_review_required')
        quote={'event_id':f'tvquote:{self.symbol}:{at}','symbol':self.symbol,'time':at,'price':event['price']}
        counts={'quote':0,'bar1':0,'bar5':0,'reason':None}
        with self.db:
            # Quote first gives runtime current price; service filters bars to active interval.
            cur=self.db.execute('INSERT OR IGNORE INTO events(channel,event_id,payload,source,observed_at) VALUES(?,?,?,?,?)',
                                ('quote',quote['event_id'],canonical(quote),SOURCE,at))
            counts['quote']=cur.rowcount
            for bar in bars:
                cur=self.db.execute('INSERT OR IGNORE INTO finalized VALUES(?,?,?)',
                                    (bar['event_id'],canonical(bar),bar['close_time']))
                if not cur.rowcount:continue
                self.db.execute('INSERT INTO events(channel,event_id,payload,source,observed_at) VALUES(?,?,?,?,?)',
                                ('bar',bar['event_id'],canonical(bar),SOURCE,at))
                counts['bar1' if bar['close_time']-bar['open_time']==1000 else 'bar5']+=1
            self.db.execute("INSERT OR REPLACE INTO cursor VALUES('previous',?)",(canonical(event),))
            self.sequence=int(self.db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0])
            # Keep > a day of typical callbacks. Consumer gaps are explicit via minimum seq.
            if self.sequence%1000 < counts['quote']+counts['bar1']+counts['bar5']:
                self.db.execute('DELETE FROM events WHERE seq<=?',(self.sequence-self.retention,))
                self.db.execute('DELETE FROM finalized WHERE close_time<?',(now_ms-86400000,))
        self.previous=event
        return counts

    def ingest_five(self,packet,now_ms):
        expected={'full_name':'CME_MINI:'+self.symbol,'minmov':25,'pricescale':100}
        h=packet.get('native5_status')
        if (packet.get('page_url')!=self.chart_url
            or packet.get('series_symbol')!='CME_MINI:'+self.symbol or packet.get('symbol_info')!=expected
            or packet.get('timeframe')!='1S' or packet.get('chart_ready') is not True
            or packet.get('chart_failed') is not False or type(packet.get('series_status')) is not int
            or packet['series_status']!=3 or packet.get('is_delayed') is not False
            or type(packet.get('replay_status')) is not int or packet['replay_status']!=0
            or packet.get('history') is not False or not isinstance(h,dict)
            or h.get('active') is not True or h.get('connected') is not True
            or h.get('resolution')!='5S' or h.get('symbol_info')!=expected
            or h.get('update_mode')!='streaming' or h.get('failed') is not False):
            raise ValueError('native_five_source_not_ready')
        stamp=datetime.fromisoformat(packet['callback_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds()!=0:
            raise ValueError('native_five_bad_clock')
        at=milliseconds(packet['callback_at'])
        if not 0<=now_ms-at<=2000:raise ValueError('native_five_stale_callback')
        old=self.db.execute("SELECT value FROM cursor WHERE key='five_at'").fetchone()
        if old and at<=int(old[0]):return {'quote':0,'bar1':0,'bar5':0,'reason':'callback_ms_not_advancing'}
        bars=bars_from_event(packet,now_ms,5000,symbol=self.symbol)
        for bar in bars:
            old=self.db.execute('SELECT payload FROM finalized WHERE event_id=?',(bar['event_id'],)).fetchone()
            if old and old[0]!=canonical(bar):
                self.changed_candle=True
                raise ValueError('finalized_native_candle_changed_restart_review_required')
        counts={'quote':0,'bar1':0,'bar5':0,'reason':None}
        with self.db:
            for bar in bars:
                cur=self.db.execute('INSERT OR IGNORE INTO finalized VALUES(?,?,?)',
                                    (bar['event_id'],canonical(bar),bar['close_time']))
                if not cur.rowcount:continue
                self.db.execute('INSERT INTO events(channel,event_id,payload,source,observed_at) VALUES(?,?,?,?,?)',
                                ('bar',bar['event_id'],canonical(bar),SOURCE,at))
                counts['bar5']+=1
            self.db.execute("INSERT OR REPLACE INTO cursor VALUES('five_at',?)",(str(at),))
            self.sequence=int(self.db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0])
        return counts

    def close(self):
        self.db.close()
