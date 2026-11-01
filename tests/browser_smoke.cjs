/* Real-service browser smoke. No mocked routes or model responses.
 * NODE_PATH=/path/to/node_modules MEMORY_OS_URL=http://127.0.0.1:8000 \
 * MEMORY_OS_API_KEY=... node tests/browser_smoke.cjs
 * Set MEMORY_OS_TEST_SEMANTIC=1 only with configured, reachable embedding API.
 */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
(async () => {
  const baseURL = process.env.MEMORY_OS_URL || 'http://127.0.0.1:8000';
  const key = process.env.MEMORY_OS_API_KEY;
  assert(key, 'MEMORY_OS_API_KEY is required');
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], failedRequests = [], badResponses = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('requestfailed', r => failedRequests.push({ url: r.url(), reason: r.failure()?.errorText }));
  page.on('response', r => { if (r.status() >= 400) badResponses.push({ url: r.url(), status: r.status() }); });
  const scope = 'browser-smoke-' + Date.now();
  const content = '浏览器验证记忆：项目沟通使用中文。';
  let deleted = false;
  const api = async (path, options={}) => {
    const response = await page.request.fetch(baseURL + '/api/v1' + path, { ...options, headers: { Authorization: 'Bearer ' + key } });
    assert(response.ok(), 'HTTP ' + response.status() + ' ' + path + ': ' + await response.text());
    return response.json();
  };
  try {
    await page.goto(baseURL);
    await page.getByLabel('项目范围', { exact: true }).fill(scope);
    await page.getByLabel('访问密钥', { exact: true }).fill(key);
    await page.getByRole('button', { name: '连接服务', exact: true }).click();
    await page.getByText('已连接', { exact: true }).waitFor();
    assert.equal(await page.locator('#stat-memories').innerText(), '0');
    await page.locator('[data-page="memories"]').click();
    await page.getByLabel('内容', { exact: true }).fill(content);
    await page.getByLabel('标签', { exact: true }).fill('偏好, browser-test');
    await page.getByLabel('使用模型自动提取').uncheck();
    await page.getByRole('button', { name: '保存记忆', exact: true }).click();
    await page.locator('#memory-list').getByText(content, { exact: true }).waitFor({ timeout: 100000 });
    await page.locator('#memory-list').getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByLabel('记忆内容', { exact: true }).fill(content + '代码审查也使用中文。');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.locator('#memory-list').getByText(content + '代码审查也使用中文。', { exact: true }).waitFor();
    if (process.env.MEMORY_OS_TEST_SEMANTIC === '1') {
      // The worker indexes asynchronously; wait for the edited version before testing the UI.
      let indexed = false;
      for (let attempt = 0; attempt < 45; attempt++) {
        const result = await api('/recall', {method:'POST', data:{scope,query:'沟通语言偏好',top_k:5}});
        if (JSON.stringify(result).includes(content + '代码审查也使用中文。')) { indexed = true; break; }
        await page.waitForTimeout(1000);
      }
      assert(indexed, 'Edited memory was not indexed within the verification window');
      await page.getByLabel('检索问题', { exact: true }).fill('沟通语言偏好');
      await page.getByRole('button', { name: '检索记忆', exact: true }).click();
      await page.locator('#recall-results').getByText(content + '代码审查也使用中文。', { exact: true }).waitFor({ timeout: 60000 });
    }
    await page.locator('[data-page="traces"]').click();
    await page.getByRole('button', { name: '刷新轨迹', exact: true }).click();
    await page.locator('[data-page="analysis"]').click();
    await page.getByRole('button', { name: '刷新分析', exact: true }).click();
    await page.locator('[data-page="injection"]').click();
    await page.getByLabel('Token 预算', { exact: true }).fill('100');
    await page.getByRole('button', { name: '组装注入内容', exact: true }).click();
    await page.locator('#inject-meta').getByText(/Token：/).waitFor({ timeout: 60000 });
    const tokenCount = Number((await page.locator('#inject-meta').innerText()).match(/Token：(\d+)/)[1]);
    assert(tokenCount <= 100, 'Injection exceeded budget');
    await page.locator('[data-page="events"]').click();
    await page.locator('#event-list details').first().waitFor();
    await page.locator('#event-list details summary').first().click();
    assert(Number(await page.locator('#event-cursor').innerText()) > 0);
    await page.locator('[data-page="memories"]').click();
    await page.locator('#memory-list').getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '取消', exact: true }).last().click();
    assert.equal(await page.locator('#memory-list article').count(), 1);
    await page.locator('#memory-list').getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.locator('#memory-list .empty').waitFor();
    deleted = true;
    // Manual execution fixtures exercise the real event API and projections, not a model or trace-provider mock.
    const traceId = randomUUID(), skill = 'browser-timeout-review';
    const event = (type, entity, payload) => ({event_id:randomUUID(),event_type:type,
      entity_type:type === 'skill.invoked'?'skill':'trace',entity_id:entity,payload});
    const fixtures = [
      event('trace.started', traceId, {task:'浏览器验证：工具超时复盘',agent_version:'manual-browser-fixture'}),
      event('trace.span.opened','agent-span',{trace_id:traceId,span_type:'agent',name:'审查 Agent'}),
      event('trace.span.opened','tool-span',{trace_id:traceId,parent_span_id:'agent-span',span_type:'tool',name:'超时工具'}),
      event('trace.span.closed','tool-span',{trace_id:traceId,status:'error',duration_ms:2000,tokens:20}),
      event('trace.error',traceId,{span_id:'tool-span',error_type:'timeout',message:'手工测试事件：工具超时'}),
      event('trace.span.closed','agent-span',{trace_id:traceId,status:'error',duration_ms:2200}),
      event('trace.finished',traceId,{status:'error',total_duration_ms:2200,total_tokens:200})
    ];
    for(let i=0;i<10;i++)fixtures.push(event('skill.invoked','skill-call-'+i,{skill_name:skill,trace_id:traceId,
      span_id:i>=8?'tool-span':'successful-span-'+i,status:i<8?'success':'error',duration_ms:100,tokens:20,
      error_type:i<8?null:'timeout'}));
    await api('/events',{method:'POST',data:{scope,events:fixtures}});
    await page.locator('[data-page="traces"]').click();
    await page.locator('#trace-list').getByText('浏览器验证：工具超时复盘',{exact:true}).waitFor();
    await page.getByRole('button',{name:'查看执行详情',exact:true}).click();
    await page.locator('#trace-detail').getByText('审查 Agent · error · 2200 ms',{exact:true}).waitFor();
    assert.equal(await page.locator('#trace-detail > .trace-tree .trace-tree').count(),1,'Nested span tree is missing');
    assert((await page.locator('#trace-detail').innerText()).includes('timeout'));
    await page.locator('[data-page="analysis"]').click();
    const skillRow=page.locator('#skill-list tbody tr').filter({hasText:skill});
    await skillRow.getByText('80.0%',{exact:true}).waitFor();
    assert((await skillRow.innerText()).includes('timeout'));
    await skillRow.getByRole('button',{name:'生成建议',exact:true}).click();
    await page.locator('#recommendation').getByText(/主要失败类型 timeout/).waitFor();
    await page.locator('[data-page="injection"]').click();
    await page.getByLabel('触发场景',{exact:true}).selectOption('skill.invoked');
    await page.getByLabel('技能名称',{exact:true}).fill(skill);
    await page.getByLabel('Token 预算',{exact:true}).fill('500');
    await page.getByRole('button',{name:'组装注入内容',exact:true}).click();
    await page.locator('#inject-result').getByText(/主要失败类型 timeout/).waitFor();
    assert((await page.locator('#inject-result').innerText()).includes(skill));
    const data=await api('/analysis?scope='+encodeURIComponent(scope));
    assert.equal(data.skills[0].evaluation.before_count,10);
    assert.equal(data.skills[0].evaluation.after_count,0);
    assert.equal(data.skills[0].evaluation.conclusion,'insufficient_evidence');
    await page.locator('[data-page="analysis"]').click();
    await page.locator('#skill-list summary').getByText('注入评估：证据不足',{exact:true}).waitFor();
    await page.locator('#skill-list summary').click();
    assert((await page.locator('#skill-list details').innerText()).includes('注入前 10 次'));
    fs.mkdirSync('output/playwright', { recursive: true });
    await page.screenshot({path:'output/playwright/dashboard-analysis.png',fullPage:true,animations:'disabled'});
    await page.locator('[data-page="memories"]').click();
    await page.locator('#memory-list').getByText(/主要失败类型 timeout/).first().waitFor();
    await page.screenshot({path:'output/playwright/dashboard-memories.png',fullPage:true,animations:'disabled'});
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile viewport overflows');
    fs.mkdirSync('output/playwright', { recursive: true });
    await page.screenshot({ path: 'output/playwright/dashboard-mobile.png', fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('[data-page="overview"]').click();
    await page.screenshot({ path: 'output/playwright/dashboard-desktop.png', fullPage: true, animations: 'disabled' });
    await page.getByRole('button', { name: '断开', exact: true }).click();
    assert.equal(await page.getByLabel('访问密钥', { exact: true }).inputValue(), '');
    assert.deepEqual(errors, [], 'Browser console/page errors');
    assert.deepEqual(failedRequests, [], 'Failed network requests');
    assert.deepEqual(badResponses, [], 'HTTP error responses');
    console.log(JSON.stringify({ passed: true, scope, semantic: process.env.MEMORY_OS_TEST_SEMANTIC === '1', manualExecutionFixtures: true, traceTree: true, skillSuccessRate: 0.8, skillInjection: true, consoleErrors: errors, failedRequests, badResponses }));
  } finally {
    // Preserve the service itself; cleanup only this smoke's memory if a failure occurred.
    if (!deleted) {
      try {
        const d = await api('/memories?scope=' + encodeURIComponent(scope));
        for (const m of d.items || []) if (!m.deleted) await api('/memories/' + encodeURIComponent(m.id) + '?scope=' + encodeURIComponent(scope), { method: 'DELETE' });
      } catch (e) { console.error('Smoke cleanup failed:', e.message); }
    }
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
