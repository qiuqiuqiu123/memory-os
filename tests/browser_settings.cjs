// Real API settings UI test. No network interception or fake model server.
// Optional MEMORY_OS_TEST_MODELS=1 exercises both real saved endpoints.
// MEMORY_OS_TEST_EMBEDDING=1 tests only the saved embedding service.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.MEMORY_OS_URL||'http://127.0.0.1:8000';
 const key=process.env.MEMORY_OS_API_KEY;assert(key,'MEMORY_OS_API_KEY required');
 const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[],failed=[];page.on('pageerror',e=>errors.push(e.message));page.on('requestfailed',r=>failed.push(r.url()));
 try{
  await page.goto(base);await page.getByLabel('访问密钥',{exact:true}).fill(key);await page.getByRole('button',{name:'连接服务',exact:true}).click();await page.getByText('已连接',{exact:true}).waitFor();
  await page.locator('[data-page="settings"]').click();
  await page.locator('#settings-form').waitFor();await page.locator('#settings-save:enabled').waitFor();
  const original=await page.request.get(base+'/api/v1/settings',{headers:{Authorization:'Bearer '+key}});assert(original.ok(),await original.text());const before=await original.json();
  assert(!JSON.stringify(before).includes('"api_key":'),'GET must not return secrets');
  assert.equal(await page.locator('#settings-llm-key').inputValue(),'');assert.equal(await page.locator('#settings-embedding-key').inputValue(),'');
  assert.equal(await page.locator('#settings-llm-model').inputValue(),before.llm.model);
  assert.equal(await page.locator('#settings-embedding-model').inputValue(),before.embedding.model);
  if(!before.llm.model)await page.locator('#settings-llm-model').fill('gpt-4.1-mini');
  if(!before.llm.base_url)await page.locator('#settings-llm-url').fill('https://api.openai.com/v1');
  if(!before.embedding.model){await page.locator('#settings-embedding-model').fill('bge-m3');await page.locator('#settings-embedding-url').fill('http://bge-m3:8080/v1');await page.locator('#settings-embedding-dimensions').fill('1024');await page.locator('#settings-reindex').check();page.once('dialog',d=>d.accept());}
  await page.getByRole('button',{name:'保存模型设置',exact:true}).click();await page.locator('#notice').getByText(/设置已保存/).waitFor();
  const afterResponse=await page.request.get(base+'/api/v1/settings',{headers:{Authorization:'Bearer '+key}});const after=await afterResponse.json();assert(after.revision>before.revision);assert.equal(after.llm.api_key_set,before.llm.api_key_set);assert.equal(after.embedding.api_key_set,before.embedding.api_key_set);
  assert.equal(await page.locator('#settings-llm-key').inputValue(),'');assert.equal(await page.locator('#settings-embedding-key').inputValue(),'');
  // Changing vector identity requires explicit consent; this attempt never writes to the API.
  await page.locator('#settings-embedding-model').fill(after.embedding.model+'-consent-check');await page.getByRole('button',{name:'保存模型设置',exact:true}).click();await page.locator('#notice').getByText(/重建/).waitFor();
  assert((await page.locator('#notice').getAttribute('class')).includes('error'));
  await page.locator('#settings-reload').click();await page.locator('#settings-embedding-model').getAttribute('value');await page.waitForFunction(model=>document.getElementById('settings-embedding-model').value===model,after.embedding.model);
  if(!after.llm.api_key_set){await page.locator('#test-llm').click();await page.locator('#settings-test-llm').getByText(/连接失败/).waitFor();assert(!(await page.locator('#settings-test-llm').innerText()).includes('连接成功'));}
  const targets=process.env.MEMORY_OS_TEST_MODELS==='1'?['embedding','llm']:(process.env.MEMORY_OS_TEST_EMBEDDING==='1'?['embedding']:[]);
  if(targets.length){
   for(const target of targets){await page.locator('#test-'+target).click();await page.locator('#settings-test-'+target).getByText(/连接成功/).waitFor({timeout:120000});}
  }
  await page.evaluate(()=>scrollTo(0,0));fs.mkdirSync('output/playwright',{recursive:true});await page.screenshot({path:'output/playwright/settings-desktop.png',fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overflow');await page.screenshot({path:'output/playwright/settings-mobile.png',fullPage:true,animations:'disabled'});
  await page.getByRole('button',{name:'断开',exact:true}).click();assert(await page.locator('#settings-save').isDisabled());assert.equal(await page.locator('#settings-llm-key').inputValue(),'');
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);console.log(JSON.stringify({passed:true,settingsSaved:true,blankKeyPreserved:true,reindexConsent:true,modelsTested:targets,missingLLMKeyRejected:!after.llm.api_key_set,errors,failed}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
