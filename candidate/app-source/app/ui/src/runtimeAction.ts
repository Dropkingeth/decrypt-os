export type RuntimeAction = (action:string, body:Record<string,unknown>)=>Promise<boolean>;
