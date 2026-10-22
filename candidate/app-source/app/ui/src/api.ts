export type EngineCatalogItem={id:string;strategyId:string;label:string;version:string|null;description:string;customerAvailability?:'available'|'coming_soon'|string;packageStatus:'not_packaged'|'source_packaged'};
export type ModuleCatalogItem={id:string;label:string;version:string;description:string;scope:'engine'|'system';runtimeSupported?:boolean};
// The packaged engine and module catalogue. Read only: installing and running is the Trading page's Execution runtime.
export type EngineCatalogState={schema:'decrypt.engine-workspace.v1';setupOnly:true;catalog:{schema:'decrypt.engine-module-catalog.v1';engines:EngineCatalogItem[];modules:ModuleCatalogItem[]}};
// What the Tradovate bridge reports after you sign in (POST /trading/bridge-login). Read only; never a token.
export type BridgeLogin={configured:boolean;account:{name:string;number:string;contract:string|null}|null;daemon:'reachable'|'unreachable';signIn:'connected'|'logged_out'|'needs_human'|'unknown';detail:string;accounts:{id:number;name:string;configured:boolean}[];nameMatch:boolean|null;numberMatch:boolean|null;guard:{state:string|null;reason:string|null}|null};
export class ApiError extends Error{constructor(message:string,public code:string,public status:number){super(message);this.name='ApiError';}}
// Strategy access: the TradingView username (and optionally picked strategy ids) the user
// sends to Decrypt Labs. strategyFieldSupported is the live-rollout switch: while false, a
// picked strategy list is recorded locally (latest.strategies) and shown back to the user,
// but the live sign-up service never receives it (it rejects unknown JSON keys today).
export type StrategyAccessState={tradingViewUsername:string;requestConfigured:boolean;strategyFieldSupported:boolean;requestableStrategies:string[];publicSite:string;latest:{tradingViewUsername:string;contact:string;status:'sending'|'sent'|'not_sent';reason:string|null;strategies:string[];createdAt:string;sentAt:string|null}|null};
export type State={engineWorkspace?:EngineCatalogState;strategyAccess:StrategyAccessState;schema:string;mode:string;onboarding:{version:number;step:string;completed:string[];tradingPreferences:boolean;preferencesComplete:boolean};profile:{assistantName:string;tradingViewUsername?:string};connections:Record<string,string>;usage:{ai:{status:string;used:number|null;limit:number|null};account:{status:string;balance:number|null;currency:string|null}};bots:{id:string;name:string;version:string|null;status:string}[];activity:{id?:string;type?:string;message?:string;createdAt?:string;timestamp?:string;[key:string]:unknown}[]};
let csrf='';
let sessionRequest:Promise<void>|null=null;
export async function api<T>(path:string,body?:unknown):Promise<T>{
 if(body!==undefined&&!csrf){sessionRequest??=api<{csrfToken:string}>('/session').then(s=>{csrf=s.csrfToken;}).finally(()=>{sessionRequest=null;});await sessionRequest;}
 const response=await fetch('/api'+path,{credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},method:body===undefined?'GET':'POST',body:body===undefined?undefined:JSON.stringify(body)});
 const result=await response.json();
 if(!response.ok){if(response.status===403)csrf='';throw new ApiError(result.error?.message||'The local service could not complete this action. Please try again.',result.error?.code||'service_error',response.status);}
 return result as T;
}
