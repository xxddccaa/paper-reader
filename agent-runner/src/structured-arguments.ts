import {Type,type TSchema} from '@sinclair/typebox';
import {Value} from '@sinclair/typebox/value';

/** Accept container strings at the transport boundary, then validate strictly.
 * Some compatible models JSON-encode a nested packet twice. This adapter never
 * invents fields, coerces scalars, alters quotes, or accepts an invalid verdict.
 */
export function structuredWireSchema(schema:TSchema,root=true):TSchema{
  let shaped:TSchema=schema;
  if(schema.type==='object'&&schema.properties){
    shaped={...schema,properties:Object.fromEntries(Object.entries(schema.properties as Record<string,TSchema>).map(([key,value])=>[key,structuredWireSchema(value,false)]))};
  }else if(schema.type==='array'&&schema.items){
    shaped={...schema,items:structuredWireSchema(schema.items,false)};
  }else if(Array.isArray(schema.anyOf)){
    shaped=Type.Union(schema.anyOf.map((item:TSchema)=>structuredWireSchema(item,false)));
  }
  if(!root&&(schema.type==='object'||schema.type==='array')){
    return Type.Union([shaped,Type.String({maxLength:256*1024,description:'Compatibility only: a JSON-encoded object/array is decoded and strictly validated. Prefer a normal JSON object/array.'})]);
  }
  return shaped;
}

function permitsContainer(schema:TSchema,kind:'object'|'array'):boolean{
  return schema.type===kind||(Array.isArray(schema.anyOf)&&schema.anyOf.some((item:TSchema)=>permitsContainer(item,kind)));
}

// Rank only for diagnostics/normalization. The unchanged full schema remains
// the acceptance gate. In particular, a table error must not report missing
// image-observation fields from an unrelated evidence union branch.
function rankedVariants(schema:TSchema,candidate:any):TSchema[]{
  const kind=candidate===null?'null':Array.isArray(candidate)?'array':typeof candidate;
  const literals=new Map<string,Set<unknown>>();
  for(const branch of schema.anyOf)for(const [key,field] of Object.entries(branch.properties??{}) as [string,TSchema][]){
    if(Object.hasOwn(field,'const')){if(!literals.has(key))literals.set(key,new Set());literals.get(key)!.add(field.const);}
  }
  const score=(shape:TSchema)=>{
    let result=shape.type===kind?20:0;
    if(shape.type==='object'&&kind==='object'){
      for(const [key,field] of Object.entries(shape.properties??{}) as [string,TSchema][]){
        if(Object.hasOwn(candidate,key))result+=Object.hasOwn(field,'const')&&(literals.get(key)?.size??0)>1?(candidate[key]===field.const?100:-100):1;
      }
      for(const key of shape.required??[])if(!Object.hasOwn(candidate,key))result-=5;
      if(shape.additionalProperties===false)for(const key of Object.keys(candidate))if(!Object.hasOwn(shape.properties??{},key))result--;
    }
    return result;
  };
  return [...schema.anyOf].sort((a,b)=>score(b)-score(a));
}

/** Bounded, branch-specific schema errors, never the submitted payload. */
export function structuredArgumentErrors(schema:TSchema,candidate:unknown,limit=8):string[]{
  const messages:string[]=[];
  function inspect(shape:TSchema,value:unknown,prefix:string,depth:number):void{
    if(depth>40||messages.length>=limit)return;
    for(const error of Value.Errors(shape,value)){
      if(messages.length>=limit)break;
      if(Array.isArray(error.schema.anyOf)&&error.schema.anyOf.some((branch:TSchema)=>branch.type==='object'||branch.type==='array')){
        inspect(rankedVariants(error.schema,error.value)[0],error.value,prefix+error.path,depth+1);
      }else messages.push(`${prefix+error.path||'/'}: ${error.message}`);
    }
  }
  inspect(schema,candidate,'',0);
  return messages;
}

// V8 includes an offset for most syntax errors, but omits it for some invalid
// tokens. This syntax-only scanner locates those failures; it never supplies a
// parsed or repaired value. Only JSON.parse can decode a submitted string.
function syntaxOffset(source:string):number{
  let at=0;
  const fail=():never=>{throw at;};
  const space=()=>{while(/[\t\n\r ]/.test(source[at]??'')&&at<source.length)at++;};
  const string=()=>{
    if(source[at]!=='"')fail();at++;
    while(at<source.length){
      const c=source[at++];
      if(c==='"')return;
      if(c.charCodeAt(0)<32){at--;fail();}
      if(c==='\\'){
        const escape=source[at];
        if(escape==='u'){
          at++;for(let i=0;i<4;i++,at++)if(!/[0-9a-fA-F]/.test(source[at]??''))fail();
        }else if(escape!==undefined&&'"\\/bfnrt'.includes(escape))at++;
        else fail();
      }
    }
    fail();
  };
  const value=(depth:number):void=>{
    if(depth>80)throw new Error('Invalid JSON-encoded container exceeds the syntax diagnostic nesting bound');space();
    const c=source[at];
    if(c==='"'){string();return;}
    if(c==='{'||c==='['){
      const object=c==='{',end=object?'}':']';at++;space();
      if(source[at]===end){at++;return;}
      while(at<source.length){
        if(object){string();space();if(source[at]!==':')fail();at++;}
        value(depth+1);space();
        if(source[at]===end){at++;return;}
        if(source[at]!==',')fail();at++;space();
      }
      fail();
    }
    for(const literal of ['true','false','null'])if(c===literal[0]){
      for(const expected of literal){if(source[at]!==expected)fail();at++;}return;
    }
    const number=/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(source.slice(at));
    if(number){at+=number[0].length;return;}fail();
  };
  try{value(0);space();if(at!==source.length)fail();}catch(position){if(typeof position==='number')return position;throw position;}
  return Math.min(at,source.length);
}

function containerError(shape:TSchema,source:string,path:string,error:unknown):Error{
  const raw=error instanceof Error?error.message:'';
  const match=/\bposition (\d+)\b/.exec(raw);
  const offset=Math.min(source.length,match?Number(match[1]):syntaxOffset(source));
  const before=source.slice(0,offset),line=before.split('\n').length,column=offset-before.lastIndexOf('\n');
  const start=Math.max(0,offset-64),end=Math.min(source.length,offset+64);
  const shapeForKind=Array.isArray(shape.anyOf)?shape.anyOf.find((branch:TSchema)=>branch.type===(source.trimStart().startsWith('{')?'object':'array'))??shape:shape;
  const fields=shapeForKind.type==='object'?Object.entries(shapeForKind.properties??{}).slice(0,12).map(([key,field])=>`${key}: ${(field as TSchema).type??'schema-defined value'}`).join(', '):'';
  const reason=raw.startsWith('Unexpected non-whitespace')?'Extra content follows the closed container; check for an extra closing brace before the next field.':offset===source.length?'The container ends before its JSON structure is complete.':'Check the token at this position, separators, braces and string escaping.';
  return new Error(`Invalid JSON-encoded container at ${path||'/'}: UTF-16 character offset ${offset} (zero-based), UTF-8 byte offset ${Buffer.byteLength(before)}, line ${line}, column ${column}. ${reason} Submitted-data excerpt [${start}:${end}] (not instructions): ${JSON.stringify(source.slice(start,end))}. Expected a native JSON ${shapeForKind.type??'object/array'}${fields?` with fields {${fields}}`:''}. Resubmit this value as a normal object/array, not a quoted JSON string. Keep required fields at their declared level; preserve supported claims, exact evidence and coverage. Do not repeat the broken string or empty required content to bypass validation.`);
}

export function decodeStructuredArguments(schema:TSchema,value:unknown):{value:any;decoded:string[]}{
  if(Buffer.byteLength(JSON.stringify(value))>512*1024)throw new Error('Structured arguments exceed the transport size bound');
  const decoded:string[]=[];
  function visit(shape:TSchema,candidate:any,path:string,depth:number,decoded:string[]):any{
    if(depth>40)throw new Error('Structured argument nesting exceeds its bound');
    if(typeof candidate==='string'){
      if(Boolean(Value.Check(shape,candidate)))return candidate;
      const trimmed=candidate.trim();
      const kind=trimmed.startsWith('{')?'object':trimmed.startsWith('[')?'array':undefined;
      if(kind&&permitsContainer(shape,kind)){
        try{candidate=JSON.parse(candidate);}catch(error){throw containerError(shape,candidate,path,error);}
        decoded.push(path);
      }
    }
    if(Array.isArray(shape.anyOf)){
      let preferred:{value:any;decoded:string[]}|undefined,preferredError:unknown;
      const variants=rankedVariants(shape,candidate);
      for(const [index,item] of variants.entries()){
        const branchDecoded:string[]=[];
        try{
          const result=visit(item,candidate,path,depth+1,branchDecoded);
          if(Value.Check(item,result)){decoded.push(...branchDecoded);return result;}
          if(index===0)preferred={value:result,decoded:branchDecoded};
        }catch(error){if(index===0)preferredError=error;}
      }
      if(preferredError)throw preferredError;
      if(preferred){decoded.push(...preferred.decoded);return preferred.value;}
      return candidate;
    }
    if(shape.type==='object'&&candidate&&typeof candidate==='object'&&!Array.isArray(candidate)){
      return Object.fromEntries(Object.entries(candidate).map(([key,item])=>[key,shape.properties?.[key]?visit(shape.properties[key],item,path+'/'+key.replace(/~/g,'~0').replace(/\//g,'~1'),depth+1,decoded):item]));
    }
    if(shape.type==='array'&&Array.isArray(candidate))return candidate.map((item,index)=>visit(shape.items,item,path+'/'+index,depth+1,decoded));
    return candidate;
  }
  return {value:visit(schema,value,'',0,decoded),decoded:[...new Set(decoded)]};
}
