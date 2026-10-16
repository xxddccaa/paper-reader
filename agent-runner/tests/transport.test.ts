import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {checkedLookup,createModelTransport,isPublicAddress} from '../src/transport.js';

function resolveWith(check:ReturnType<typeof checkedLookup>){return new Promise<any[]>((resolve,reject)=>check('model.example',{all:true},(error:Error|null,addresses:any[])=>error?reject(error):resolve(addresses)));}

test('global endpoint classification rejects private, reserved and mapped-private addresses',()=>{
  for(const address of ['127.0.0.1','10.2.3.4','169.254.169.254','100.64.0.1','192.168.1.1','192.0.2.1','0.0.0.0','224.1.1.1','::1','fc00::1','fe80::1','::ffff:127.0.0.1','2001:db8::1','4000::1','::2'])assert.equal(isPublicAddress(address),false,address);
  for(const address of ['8.8.8.8','1.1.1.1','2606:4700:4700::1111'])assert.equal(isPublicAddress(address),true,address);
});
test('connection DNS revalidates instead of trusting an earlier public preflight',async()=>{
  let request=0;const check=checkedLookup(false,(_host,callback)=>{request++;callback(null,[{address:request===1?'8.8.8.8':'127.0.0.1',family:4}]);});
  assert.deepEqual(await resolveWith(check),[{address:'8.8.8.8',family:4}]);
  await assert.rejects(resolveWith(check),/non-public/);
});
test('mixed DNS answers fail closed; private resolution needs explicit deployment opt-in',async()=>{
  const resolver=(_host:string,callback:any)=>callback(null,[{address:'8.8.8.8',family:4},{address:'10.0.0.1',family:4}]);
  await assert.rejects(resolveWith(checkedLookup(false,resolver)),/non-public/);
  assert.equal((await resolveWith(checkedLookup(true,resolver))).length,2);
});
test('actual transport cannot contact private literals by default or follow redirects',async()=>{
  assert.throws(()=>createModelTransport('http://127.0.0.1:1234/v1'),/Private/);
  let targetRequests=0;
  const server=createServer((req,res)=>{if(req.url==='/v1/redirect'){res.writeHead(302,{Location:'/v1/target'});res.end();}else{targetRequests++;res.end('ok');}});
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  const transport=createModelTransport(`http://127.0.0.1:${port}/v1`,true);
  try{
    await assert.rejects(transport.fetch(`http://127.0.0.1:${port}/v1/redirect`),/redirects/);
    assert.equal(targetRequests,0);
    await assert.rejects(transport.fetch(`http://127.0.0.1:${port}/admin`),/escaped/);
    await assert.rejects(transport.fetch('http://169.254.169.254/latest/meta-data'),/escaped/);
  }finally{await transport.close();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));}
});

test('actual sockets use the guarded lookup and cannot dial a rebound private target',async()=>{
  let requests=0,lookups=0;
  const server=createServer((_req,res)=>{requests++;res.end('allowed only with deployment opt-in');});
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  const resolver=(_host:string,callback:any)=>{lookups++;callback(null,[{address:'127.0.0.1',family:4}]);};
  const blocked=createModelTransport(`http://model.example:${port}/v1`,false,resolver);
  const allowed=createModelTransport(`http://model.example:${port}/v1`,true,resolver);
  try{
    await assert.rejects(blocked.fetch(`http://model.example:${port}/v1/chat/completions`));
    assert.equal(requests,0);assert.equal(lookups,1);
    const response=await allowed.fetch(`http://model.example:${port}/v1/chat/completions`);
    assert.equal(await response.text(),'allowed only with deployment opt-in');
    assert.equal(requests,1);assert.equal(lookups,2);
  }finally{await blocked.close();await allowed.close();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));}
});

test('fractional remaining-time milliseconds support actual HTTP headers and streamed bodies',async()=>{
  let requests=0;
  const server=createServer((_req,res)=>{
    requests++;res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('data: first\n\n');
    setImmediate(()=>res.end('data: complete\n\n'));
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  const transport=createModelTransport(`http://127.0.0.1:${port}/v1`,true,undefined,298123.25);
  try{
    const response=await transport.fetch(`http://127.0.0.1:${port}/v1/chat/completions`);
    assert.equal(response.status,200);assert.equal(await response.text(),'data: first\n\ndata: complete\n\n');assert.equal(requests,1);
  }finally{await transport.close();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));}
});

test('timeout normalization does not round invalid values into the allowed range',()=>{
  for(const timeout of [0.99,301000.01,Number.NaN,Number.POSITIVE_INFINITY]){
    assert.throws(()=>createModelTransport('http://127.0.0.1:1/v1',true,undefined,timeout),/Invalid model stream timeout/);
  }
});
