// Real API/PG browser test. The review is an explicit manual fixture, never an LLM response mock.
// MEMORY_OS_URL supports / or /memoryos/. Seed only an isolated debug/test database.
// Required: MEMORY_OS_API_KEY and either MEMORY_OS_DATABASE_URL or CAPTURE_SESSION_ID+CAPTURE_SCOPE.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {randomUUID}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
(async()=>{
 const base=(process.env.MEMORY_OS_URL||'http://127.0.0.1:8000').replace(/\/?$/,'/');
 const key=process.env.MEMORY_OS_API_KEY;assert(key,'MEMORY_OS_API_KEY required');
 const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1050}});
 const errors=[],failed=[],bad=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});page.on('requestfailed',r=>failed.push(r.url()));page.on('response',r=>{if(r.status()>=400)bad.push({status:r.status(),url:r.url()})});
 const api=async(path,options={})=>{const r=await page.request.fetch(base+'api/v1'+path,{...options,headers:{Authorization:'Bearer '+key}});assert(r.ok(),r.status()+' '+await r.text());return r.json()};
 const realReview=process.env.CAPTURE_REAL_REVIEW==='1';
 let scope=process.env.CAPTURE_SCOPE||'capture-browser-'+Date.now(),sid=process.env.CAPTURE_SESSION_ID;
 try{
  if(!sid){
   assert(process.env.MEMORY_OS_DATABASE_URL,'Provide an isolated MEMORY_OS_DATABASE_URL to seed the explicit manual review fixture');
   const result=await api('/capture/batches',{method:'POST',data:{scope,source:'codex',source_session_id:'manual-browser-fixture-'+randomUUID(),batch_id:randomUUID(),turn_complete:false,events:[
    {source_id:'fixture-user',type:'user_message',timestamp:new Date().toISOString(),body:{text:'手工测试采集事件：分析工具超时',api_key:'sk-fixture-credential-never-display'}},
    {source_id:'fixture-tool',type:'tool_result',timestamp:new Date().toISOString(),body:{name:'read_file',error:'timeout',text:'手工测试截断内容'.repeat(3000)}},
    {source_id:'fixture-end',type:'turn_completed',timestamp:new Date().toISOString(),body:{text:'手工测试采集事件：执行结束，结果待核验'}}]}});
   sid=result.session_id;
   const python=process.env.MEMORY_OS_PYTHON||'.venv/bin/python';
   const seed=`import json,sys,uuid,psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
x=json.load(sys.stdin)
with psycopg.connect(x['database'],row_factory=dict_row) as c:
 s=c.execute('SELECT * FROM capture_sessions WHERE id=%s',(x['session_id'],)).fetchone()
 assert s and s['scope']==x['scope']
 rid=str(uuid.uuid4());cid=str(uuid.uuid4());jid=str(uuid.uuid4())
 result={'summary':'手工浏览器复盘样例：工具超时，真实任务结果尚不能确定。','findings':[{'text':'手工样例发现：工具返回超时，需核验实际执行状态。','evidence_ids':['fixture-tool']}],'optimizations':[{'text':'手工样例建议：重试前检查幂等性并保留日志。','evidence_ids':['fixture-tool']}],'memory_candidates':[{'content':'手工候选经验：工具超时后先核验执行状态，再决定是否重试。','tags':['manual-browser-fixture'],'evidence_ids':['fixture-tool']}],'evidence_ids':['fixture-tool']}
 c.execute('INSERT INTO capture_reviews(id,session_id,user_id,scope,start_seq,end_seq,result,model) VALUES(%s,%s,%s,%s,1,3,%s,%s)',(rid,s['id'],s['user_id'],s['scope'],Jsonb(result),'manual-browser-fixture-no-llm'))
 c.execute('INSERT INTO capture_candidates(id,review_id,user_id,scope,content,tags,evidence_ids) VALUES(%s,%s,%s,%s,%s,%s,%s)',(cid,rid,s['user_id'],s['scope'],result['memory_candidates'][0]['content'],Jsonb(['manual-browser-fixture']),Jsonb(['fixture-tool'])))
 c.execute('INSERT INTO jobs(id,user_id,scope,kind,payload,status,attempts,error,dedupe) VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s)',(jid,s['user_id'],s['scope'],'capture_review',Jsonb({'session_id':str(s['id']),'start_seq':1,'end_seq':3}),'failed','手工测试失败状态：连接超时，尚无自动复盘结果','manual-browser-fixture:'+jid))
print(json.dumps({'review_id':rid,'candidate_id':cid}))`;
   const seeded=execFileSync(python,['-c',seed],{input:JSON.stringify({database:process.env.MEMORY_OS_DATABASE_URL,session_id:sid,scope}),encoding:'utf8'});assert(JSON.parse(seeded).candidate_id);
  }
  await page.goto(base);await page.getByLabel('项目范围',{exact:true}).fill(scope);await page.getByLabel('访问密钥',{exact:true}).fill(key);await page.getByRole('button',{name:'连接服务',exact:true}).click();await page.getByText('已连接',{exact:true}).waitFor();
  await page.locator('[data-page="capture"]').click();await page.getByRole('button',{name:'查看会话复盘',exact:true}).first().click();
  await page.locator('#capture-session-meta').getByText(/来源 codex/).waitFor();
  let approved=false;
  if(realReview){
   await page.locator('#capture-events summary').first().waitFor();
   for(let i=0;i<60;i++){if(await page.locator('#capture-reviews article').count())break;await new Promise(r=>setTimeout(r,2000));await page.locator('#refresh-capture-detail').click()}
   assert(await page.locator('#capture-reviews article').count(),'No review available within 120 seconds');
   const links=page.locator('#capture-reviews a');if(await links.count())await links.first().click();
   if(await page.getByRole('button',{name:'批准为记忆',exact:true}).count()){
    await page.getByRole('button',{name:'批准为记忆',exact:true}).first().click();await page.locator('#capture-reviews').getByText(/已批准为记忆/).first().waitFor();approved=true;
   }
  }else{
   await page.locator('#capture-events summary').filter({hasText:'已脱敏'}).waitFor();await page.locator('#capture-events summary').filter({hasText:'已截断'}).waitFor();
   assert(!(await page.locator('#capture-events').innerText()).includes('sk-fixture-credential-never-display'));
   await page.locator('#capture-reviews').getByText(/手工浏览器复盘样例/).waitFor();
   await page.locator('#capture-reviews').getByText(/后台复盘任务：处理失败/).waitFor();
   await page.locator('#capture-reviews').getByText(/手工测试失败状态/).waitFor();
   const evidence=page.locator('#capture-reviews a').filter({hasText:'fixture-tool'}).first();await evidence.click();assert(await page.locator('#capture-event-fixture-tool').evaluate(e=>e.open));
   const before=await api('/memories?scope='+encodeURIComponent(scope));assert(!before.items.some(m=>m.content.includes('手工候选经验')),'Candidate entered memory before approval');
   await page.getByRole('button',{name:'批准为记忆',exact:true}).first().click();await page.locator('#capture-reviews').getByText(/已批准为记忆/).waitFor();
   const after=await api('/memories?scope='+encodeURIComponent(scope));assert(after.items.some(m=>m.content.includes('手工候选经验')),'Approved candidate is missing from memory');
   assert.equal(await page.getByRole('button',{name:'批准为记忆',exact:true}).count(),0);approved=true;
  }
  fs.mkdirSync('output/playwright',{recursive:true});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:'output/playwright/capture-desktop.png',fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overflow');await page.screenshot({path:'output/playwright/capture-mobile.png',fullPage:true,animations:'disabled'});
  await page.getByLabel('项目范围',{exact:true}).fill(scope+'-empty');await page.getByLabel('项目范围',{exact:true}).press('Tab');await page.locator('#capture-sessions .empty').waitFor();assert(await page.locator('#capture-detail-card').isHidden(),'Previous scope evidence remained visible');
  await page.getByRole('button',{name:'断开',exact:true}).click();assert(await page.locator('#capture-detail-card').isHidden());
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);assert.deepEqual(bad,[]);console.log(JSON.stringify({passed:true,scope,session_id:sid,manualReviewFixture:!realReview,externalLLM:realReview,redaction:!realReview,truncation:!realReview,evidenceLinks:true,approval:approved,errors,failed,bad}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
