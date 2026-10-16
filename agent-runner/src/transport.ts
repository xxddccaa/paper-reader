import {lookup as systemLookup, type LookupAddress} from 'node:dns';
import {isIP} from 'node:net';
import {Agent, fetch as undiciFetch} from 'undici';
import ipaddr from 'ipaddr.js';

export type ResolveAll=(hostname:string,callback:(error:NodeJS.ErrnoException|null,addresses:LookupAddress[])=>void)=>void;
const defaultResolve:ResolveAll=(hostname,callback)=>systemLookup(hostname,{all:true,verbatim:true},callback);
const bareHost=(host:string)=>host.replace(/^\[|\]$/g,'');
export function isPublicAddress(address:string):boolean {
  try {
    let parsed=ipaddr.parse(bareHost(address));
    if(parsed.kind()==='ipv6'&&(parsed as ipaddr.IPv6).isIPv4MappedAddress())parsed=(parsed as ipaddr.IPv6).toIPv4Address();
    if(parsed.kind()==='ipv6'&&!(parsed as ipaddr.IPv6).match(ipaddr.parse('2000::') as ipaddr.IPv6,3))return false;
    return parsed.range()==='unicast';
  }catch{return false;}
}
/** Resolve once at socket connection and dial exactly the inspected response. */
export function checkedLookup(allowPrivate:boolean,resolveAll:ResolveAll=defaultResolve) {
  return (hostname:string,options:any,callback:any)=>{
    resolveAll(hostname,(error,addresses)=>{
      if(error){callback(error);return;}
      if(!addresses.length||(!allowPrivate&&addresses.some(item=>!isPublicAddress(item.address)))){
        callback(new Error('Model endpoint DNS resolved to a non-public address'));return;
      }
      const family=typeof options==='number'?options:options?.family;
      const candidates=family===4||family===6?addresses.filter(item=>item.family===family):addresses;
      if(!candidates.length){callback(new Error('No model endpoint address for requested family'));return;}
      // net.connect with autoSelectFamily requests all addresses. These exact,
      // checked values are used by undici; there is no second DNS lookup.
      if(options?.all)callback(null,candidates);else callback(null,candidates[0].address,candidates[0].family);
    });
  };
}
export function createModelTransport(baseURL:string,allowPrivateModels=false,resolveAll?:ResolveAll,streamTimeoutMs=120000){
  const base=new URL(baseURL);
  if(!['http:','https:'].includes(base.protocol)||base.username||base.password)throw new Error('Invalid model endpoint');
  const hostname=bareHost(base.hostname);
  if(isIP(hostname)&&!allowPrivateModels&&!isPublicAddress(hostname))throw new Error('Private model endpoint is disabled');
  if(!Number.isFinite(streamTimeoutMs)||streamTimeoutMs<1||streamTimeoutMs>301000)throw new Error('Invalid model stream timeout');
  // Remaining task seconds can be fractional. Undici requires integer timeout
  // milliseconds; round down only after validation so the requested deadline
  // is never extended and invalid out-of-range values cannot be rounded in.
  const timeoutMs=Math.floor(streamTimeoutMs);
  const dispatcher=new Agent({connect:{lookup:checkedLookup(allowPrivateModels,resolveAll)},headersTimeout:timeoutMs,bodyTimeout:timeoutMs});
  const prefix=base.pathname.replace(/\/+$/,'');
  const fetch:typeof globalThis.fetch=async(input,init)=>{
    const request=new Request(input,init);
    const url=new URL(request.url);
    if(url.origin!==base.origin||(prefix&&url.pathname!==prefix&&!url.pathname.startsWith(prefix+'/'))||url.username||url.password)throw new Error('Model request escaped configured endpoint');
    const target=bareHost(url.hostname);
    if(isIP(target)&&!allowPrivateModels&&!isPublicAddress(target))throw new Error('Private model endpoint is disabled');
    const response=await undiciFetch(request.url,{method:request.method,headers:[...request.headers],
      body:['GET','HEAD'].includes(request.method)?undefined:request.body as any,
      signal:request.signal,duplex:'half',redirect:'manual',dispatcher});
    if(response.status>=300&&response.status<400){await response.body?.cancel();throw new Error('Model endpoint redirects are prohibited');}
    return response as unknown as Response;
  };
  return {fetch,close:()=>dispatcher.close()};
}
