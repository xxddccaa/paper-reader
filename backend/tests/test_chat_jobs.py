"""Chat through real isolated PostgreSQL, worker leases and JSONL subprocesses.

The child below is explicitly a protocol fixture, not a real model invocation.
"""
from copy import deepcopy
from datetime import timedelta
import json
import threading
import time

import pytest
from sqlalchemy import select

from app.chat_jobs import ChatStream, history_content, persist_chat_event
from app.config import get_settings
from app.db import SessionLocal
from app.models import ChatConversation, ChatMessage, Job, JobEvent, Membership, Reading, Revision, new_id, now
from app.worker import Worker, claim_job, LeaseLost
from test_worker_durability import worker_db  # noqa: F401
from test_workflow import guided_fixture, snapshot


CHAT_SCRIPT = r'''
const fs=require('fs'),readline=require('readline');
const send=x=>process.stdout.write(JSON.stringify(x)+'\n');
let output, wait=false, q=[];
const step=()=>{if(q.length)send({type:'tool_request',id:String(q.length),...q.shift()});else if(!wait){send({type:'usage',usage:{input_tokens:8,output_tokens:5}});send({type:'result',result:output});process.exit(0);}};
readline.createInterface({input:process.stdin}).on('line',line=>{
 const m=JSON.parse(line);if(m.type==='cancel')process.exit(0);
 if(m.type==='tool_response'){if(m.error){send({type:'error',message:m.error});process.exit(1);}step();return;}
 if(m.type!=='run')return;
 const {operation,input}=m.chat;
 const behavior=JSON.parse(fs.readFileSync(__dirname+'/chat-behavior.json'));
 fs.appendFileSync(__dirname+'/chat-calls.jsonl',JSON.stringify({operation,input})+'\n');
 if(operation==='answer'){
   const source=input.source_sections.find(s=>s.text.includes('The method improves accuracy'));
   const evidence={id:'E1',source_id:'main',section_id:source.id||source.section_id,quote:'The method improves accuracy from 70 percent to 75 percent.',...(source.page?{page:source.page}:{})};
   output={answer_markdown:'准确率从70%提高到75%。[E1](evidence:E1)',evidence:[evidence],limitations:[]};
   send({type:'chat_delta',text:'正在生成的草稿'});
   wait=behavior.wait===true;
   q=[{name:behavior.forbidden?'write_artifact':'read_source',args:behavior.forbidden?{path:'bad.txt',content:'bad'}:{source_id:'main'}},{name:'host_chat_submission',args:{}}];
 }else if(operation==='review'){output={passed:true,issues:[]};q=[{name:'host_chat_submission',args:{}}];}
 else {output={summary:'Earlier question was about accuracy.'};q=[{name:'host_chat_submission',args:{}}];}
 if(wait)setInterval(()=>{},1000);
 step();
});
'''


def test_stream_redacts_credentials_split_across_provider_chunks(monkeypatch):
    records=[]
    monkeypatch.setattr('app.chat_jobs.persist_chat_event',lambda jid,token,event: records.append(event))
    stream=ChatStream('job','token','private-secret')
    stream.last_flush=0
    stream.emit({'type':'delta','text':'visible private-'})
    stream.last_flush=0
    stream.emit({'type':'delta','text':'secret answer'})
    stream.flush(force=True)
    text=''.join(row['text'] for row in records)
    assert text=='visible [REDACTED] answer'


def test_same_citation_id_in_distinct_history_turns_keeps_original_locator():
    first=ChatMessage(role='assistant',sequence=2,content='第一篇结论[E1](evidence:E1)',
        evidence_index=[{'id':'E1','source_id':'main','section_id':'p5','page':5}])
    second=ChatMessage(role='assistant',sequence=4,content='另一篇结论[E1](evidence:E1)',
        evidence_index=[{'id':'E1','source_id':'related:arxiv:2401.12345','section_id':'p2','page':2}])
    history=[history_content(first),history_content(second)]
    assert '"message_sequence": 2' in history[0] and '"source_id": "main"' in history[0]
    assert '"message_sequence": 4' in history[1] and 'related:arxiv:2401.12345' in history[1]
    assert '"section_id": "p2"' in history[1] and '不代替原文证据' in history[1]


def test_repair_keeps_prior_draft_until_replacement_text_arrives(monkeypatch):
    records=[]
    monkeypatch.setattr('app.chat_jobs.persist_chat_event',lambda jid,token,event: records.append(event))
    stream=ChatStream('job','token','')
    stream.emit({'type':'replace','content':'已有可用回答'})
    stream.start_answer()
    stream.emit({'type':'progress','stage':'repair','message':'修复中'})
    stream.flush(force=True)
    # A timeout or model error before its first delta cannot erase the answer.
    assert [r for r in records if r['type']=='replace']==[{'type':'replace','content':'已有可用回答'}]
    stream.emit({'type':'delta','text':'修复后的回答'})
    stream.flush(force=True)
    assert records[-2:]==[{'type':'replace','content':''},{'type':'delta','text':'修复后的回答'}]
    stream.start_answer()
    stream.emit({'type':'replace','content':'最终完整回答'})
    assert stream.reset_on_delta is False


def conversation_fixture(root, **behavior):
    original, script_dir = guided_fixture(root)
    Worker().run(once=True)
    assert snapshot(original.id).status == "succeeded"
    with SessionLocal() as db, db.begin():
        reading = db.get(Reading, original.reading_id)
        db.add(Membership(workspace_id=original.workspace_id, user_id=original.created_by, role="admin"))
        conv = ChatConversation(workspace_id=original.workspace_id, reading_id=reading.id,
            user_id=original.created_by, revision_id=reading.current_revision_id, title="Test follow-up")
        db.add(conv); db.flush()
        cid = conv.id
    (script_dir / "index.js").write_text(CHAT_SCRIPT)
    (script_dir / "chat-behavior.json").write_text(json.dumps(behavior))
    return original, cid, script_dir


def enqueue(original, cid, sequence=1, question="准确率提高了多少？"):
    with SessionLocal() as db, db.begin():
        conv = db.get(ChatConversation, cid)
        user = ChatMessage(conversation_id=cid, workspace_id=original.workspace_id, role="user",
            sequence=sequence, status="succeeded", content=question, request_id=new_id())
        answer = ChatMessage(conversation_id=cid, workspace_id=original.workspace_id, role="assistant", sequence=sequence+1)
        db.add_all([user, answer]); db.flush()
        source = db.get(Job, original.id)
        job = Job(workspace_id=original.workspace_id, reading_id=original.reading_id, created_by=original.created_by,
            conversation_id=cid, kind="chat", input_snapshot={**deepcopy(source.input_snapshot),
                "base_revision_id": conv.revision_id, "chat":{"conversation_id":cid,"message_id":answer.id,"user_message_id":user.id}})
        db.add(job); db.flush(); answer.job_id=job.id
        return job.id, answer.id


def test_chat_worker_publishes_private_answer_restores_history_and_preserves_revision(worker_db):
    original, cid, script = conversation_fixture(worker_db)
    with SessionLocal() as db:
        initial_revision = db.get(Reading, original.reading_id).current_revision_id
    jid, mid = enqueue(original, cid)
    Worker().run(once=True)
    assert snapshot(jid).status == "succeeded", snapshot(jid).error
    with SessionLocal() as db:
        answer = db.get(ChatMessage, mid)
        assert answer.status == "succeeded" and answer.evidence_index[0]["verified"] is True
        assert answer.content.startswith("准确率")
        assert answer.artifacts and all('/conversations/'+cid+'/' in a['path'] for a in answer.artifacts)
        assert db.get(Reading, original.reading_id).current_revision_id == initial_revision
        assert len(list(db.scalars(select(Revision).where(Revision.reading_id==original.reading_id))))==1
        events=list(db.scalars(select(JobEvent).where(JobEvent.job_id==jid).order_by(JobEvent.id)))
        assert next(e for e in events if e.event_type=='delta').id < next(e for e in events if e.event_type=='completed').id
        assert events[-1].event_type=='completed'
    cache=get_settings().runtime_dir/'storage/workspaces'/original.workspace_id/'conversations'/cid/'sources'
    cached_files={str(path.relative_to(cache)) for path in cache.rglob('*') if path.is_file()}
    next_jid, _ = enqueue(original,cid,3,"那这个提升可以推广到所有模型吗？")
    Worker().run(once=True)
    assert snapshot(next_jid).status=='succeeded', snapshot(next_jid).error
    calls=[json.loads(line) for line in (script/'chat-calls.jsonl').read_text().splitlines()]
    second=[c for c in calls if c['operation']=='answer'][1]
    assert [h['role'] for h in second['input']['history']]==['user','assistant']
    assert '准确率' in second['input']['history'][1]['content']
    assert '"message_sequence": 2' in second['input']['history'][1]['content']
    assert cached_files=={str(path.relative_to(cache)) for path in cache.rglob('*') if path.is_file()}


def test_interrupted_publication_cache_is_reusable_by_same_job_next_attempt(worker_db,monkeypatch):
    import app.chat_jobs as chat_jobs
    original,cid,_=conversation_fixture(worker_db)
    jid,mid=enqueue(original,cid)
    store=chat_jobs._source_artifacts
    def interrupted(pipeline,target):
        store(pipeline,target)
        raise RuntimeError('Simulated process interruption after source copy')
    monkeypatch.setattr(chat_jobs,'_source_artifacts',interrupted)
    Worker().run(once=True)
    assert snapshot(jid).status=='failed'
    cache=get_settings().runtime_dir/'storage/workspaces'/original.workspace_id/'conversations'/cid/'sources'
    saved={str(p.relative_to(cache)) for p in cache.rglob('*') if p.is_file()}
    assert saved
    # Reproduce the queued state produced by abandoned-lease recovery for the
    # SAME Job ID; a normal explicit retry would hide a per-job path collision.
    with SessionLocal() as db,db.begin():
        db.get(Job,jid).status='queued'
        db.get(ChatMessage,mid).status='queued'
    monkeypatch.setattr(chat_jobs,'_source_artifacts',store)
    Worker().run(once=True)
    assert snapshot(jid).status=='succeeded',snapshot(jid).error
    assert snapshot(jid).attempt==2
    assert saved=={str(p.relative_to(cache)) for p in cache.rglob('*') if p.is_file()}


def test_chat_cancel_keeps_streamed_draft_and_fences_old_writes(worker_db):
    original,cid,_=conversation_fixture(worker_db,wait=True)
    jid,mid=enqueue(original,cid)
    job=claim_job();assert job.id==jid
    worker=Worker();thread=threading.Thread(target=worker.run_job,args=(job,));thread.start()
    try:
        for _ in range(100):
            with SessionLocal() as db:
                if db.get(ChatMessage,mid).content:break
            time.sleep(.05)
        else:pytest.fail('No draft streamed before completion')
        with SessionLocal() as db,db.begin():db.get(Job,jid).cancel_requested=True
        thread.join(8);assert not thread.is_alive()
        with SessionLocal() as db:
            answer=db.get(ChatMessage,mid)
            assert answer.status=='cancelled' and answer.content=='正在生成的草稿'
        with pytest.raises(LeaseLost):persist_chat_event(jid,job.lease_token,{'type':'delta','text':'stale'})
    finally:
        worker.stop();thread.join(5)


def test_chat_host_rejects_unregistered_artifact_tool(worker_db):
    original,cid,_=conversation_fixture(worker_db,forbidden=True)
    jid,mid=enqueue(original,cid)
    Worker().run(once=True)
    assert snapshot(jid).status=='failed'
    with SessionLocal() as db:assert db.get(ChatMessage,mid).status=='failed'
    assert not list((get_settings().runtime_dir/'tmp'/jid).rglob('bad.txt'))


def test_chat_stays_bound_to_historical_revision(worker_db):
    original,cid,_=conversation_fixture(worker_db)
    jid,_=enqueue(original,cid)
    with SessionLocal() as db,db.begin():
        reading=db.get(Reading,original.reading_id)
        old=db.get(Revision,reading.current_revision_id)
        other=Revision(reading_id=reading.id,workspace_id=reading.workspace_id,number=2,brief_markdown='new',full_markdown='new')
        db.add(other);db.flush();reading.current_revision_id=other.id
        old_id=old.id
    Worker().run(once=True)
    assert snapshot(jid).status=='succeeded',snapshot(jid).error
    with SessionLocal() as db:assert db.get(ChatConversation,cid).revision_id==old_id


def test_expired_chat_lease_updates_message_and_can_recover(worker_db):
    original,cid,_=conversation_fixture(worker_db)
    jid,mid=enqueue(original,cid)
    old=claim_job()
    with SessionLocal() as db,db.begin():db.get(Job,jid).lease_expires_at=now()-timedelta(seconds=1)
    recovered=claim_job();assert recovered.id==jid and recovered.attempt==2
    with pytest.raises(LeaseLost):persist_chat_event(jid,old.lease_token,{'type':'delta','text':'old'})
    Worker().run_job(recovered)
    assert snapshot(jid).status=='succeeded',snapshot(jid).error
    with SessionLocal() as db:assert db.get(ChatMessage,mid).status=='succeeded'
