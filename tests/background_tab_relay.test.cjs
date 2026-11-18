const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../wsa-browser-bridge/background.js'), 'utf8');

function virtualizedCompletedResponse() {
  const {run} = harness();
  run(`
    globalThis.location = {href: 'https://chatgpt.com/c/test'};
    _installWsaDomUtilsFn();
    const answer = {turnKey: 'new-id', text: 'same tool call', thinking: '', hasActionBar: true};
    const rt = {
      turns: {test: {startedAt: Date.now(), lastChangeAt: Date.now(), beforeTurnCount: 20,
        beforeTurnKeys: ['old-id'], beforeLastText: 'same tool call', beforeLastThinking: '',
        lastText: '', lastThinking: '', sawEditorCleared: true, sawStop: false}},
      readAllTurns: () => [answer], hasStopButton: () => false,
      findEditor: () => null, readModelBadge: () => 'ChatGPT', readErrorBanner: () => ''
    };
    const first = __wsaDomUtils.pollTurn(rt, 'test');
    if (first.text !== 'same tool call') throw Error('Missed completed virtualized response');
    rt.turns.test.lastChangeAt = Date.now() - 2000;
    if (!__wsaDomUtils.pollTurn(rt, 'test').done) throw Error('Completed response did not settle');
    rt.turns.test = {startedAt: Date.now(), lastChangeAt: Date.now(), beforeTurnCount: 20,
      beforeTurnKeys: ['new-id'], beforeLastText: 'same tool call', lastText: '', lastThinking: '',
      sawEditorCleared: true, sawStop: false};
    if (__wsaDomUtils.pollTurn(rt, 'test').text) throw Error('Replayed historical response');
  `);
}
virtualizedCompletedResponse();

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function harness(initial = {}, liveEvents = false) {
  const storage = structuredClone(initial);
  const timers = [];
  const updates = [];
  const messages = [];
  const listeners = {};
  const event = name => ({addListener:callback=>{listeners[name]=callback;}});
  const ctx = vm.createContext({
    console, URL, URLSearchParams, AbortSignal, Date, structuredClone,
    setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; }, clearTimeout() {},
    chrome: {
      storage: { local: {
        get: async keys => Object.fromEntries(keys.map(k => [k, structuredClone(storage[k])])),
        set: async values => Object.assign(storage, structuredClone(values)),
        remove: async keys => keys.forEach(k => delete storage[k]),
      }},
      runtime: {id:liveEvents ? 'test-extension' : undefined,
                sendMessage: async message => { messages.push(message); },
                onInstalled:event('installed'), onStartup:event('startup'),
                onConnect:event('connect'), onMessage:event('message')},
      alarms: {create(){}, onAlarm:event('alarm')},
      tabs: {get: async id => ({id, active:false, autoDiscardable:false, url:'https://chatgpt.com/c/test'}),
             update: async (id, values) => {updates.push(values); return {id, ...values};}},
    },
    fetch: async () => ({ok:true, json:async()=>({ok:true}), headers:{get:()=>null}}),
  });
  vm.runInContext(source, ctx);
  return {ctx, storage, timers, updates, messages, listeners, run:code=>vm.runInContext(code,ctx)};
}
const job = {reqId:'test-job', tabId:7, clientId:'brave-test', provider:'chatgpt', startedAt:Date.now(), lastProgressAt:Date.now()};

async function acquisitionIsSingleFlight() {
  const h = harness();
  const gate = deferred();
  let scans = 0;
  h.ctx.scanOpenSiteStates = async () => {scans++; await gate.promise; return {ident:{clientId:'test'}, hasAnyAiTab:false};};
  const first = h.run('checkAndRelay()');
  const second = h.run('checkAndRelay()');
  await new Promise(setImmediate);
  assert.equal(scans, 1, 'two idle requests must never acquire simultaneously');
  await h.run('pollActiveRelayJob()');
  assert.equal(h.run('injecting'), true, 'a poll while acquiring must not clear ownership');
  gate.resolve();
  await Promise.all([first, second]);
  assert.equal(h.run('injecting'), false);
}

async function completionWaitsForAcknowledgement() {
  const h = harness({activeRelayJob:job});
  h.run('injecting = true');
  h.ctx.resolveFinalChatUrl = async () => 'https://chatgpt.com/c/test';
  const ack = deferred();
  h.ctx.fetch = async () => {await ack.promise; return {ok:true,json:async()=>({ok:true})};};
  const completing = h.run('completeActiveRelayJob(' + JSON.stringify(job) + ', {text:"done",thinking:"",done:true})');
  await new Promise(setImmediate);
  assert.ok(h.storage.activeRelayJob, 'pending completion must stay durable until acknowledgement');
  assert.equal(h.run('injecting'), true, 'completion must not expose an idle poll window');
  ack.resolve();
  await completing;
  assert.equal(h.storage.activeRelayJob, undefined);
  assert.equal(h.run('injecting'), false);
  assert.ok(h.updates.every(u=>u.active !== true), 'never steal tab focus');
}

async function failedDeliverySurvivesRestart() {
  const h = harness({activeRelayJob:job});
  h.ctx.resolveFinalChatUrl = async () => 'https://chatgpt.com/c/test';
  h.ctx.fetch = async () => {throw new Error('temporary network failure');};
  await h.run('completeActiveRelayJob(' + JSON.stringify(job) + ', {text:"saved result",done:true})').catch(()=>{});
  assert.equal(h.storage.activeRelayJob?.completion?.response, 'saved result');
  const restored = harness(h.storage);
  let delivered = null;
  restored.ctx.fetch = async (url, opts) => {
    assert.ok(url.endsWith('/pending-response'));
    delivered = JSON.parse(opts.body);
    return {ok:true,json:async()=>({ok:true})};
  };
  await restored.run('restoreActiveRelayJob()');
  await restored.run('pollActiveRelayJob()');
  assert.equal(delivered.response, 'saved result');
  assert.equal(restored.storage.activeRelayJob, undefined);
}

async function rejectedAndInvalidAcknowledgements() {
  for (const response of [{ok:false, status:503}, {ok:true, json:async()=>({})},
                          {ok:true, json:async()=>({ok:false})}]) {
    const pending = {...job, completion:{id:job.reqId, response:'do not lose me'}};
    const h = harness({activeRelayJob:pending});
    h.ctx.fetch = async () => response;
    await h.run('pollActiveRelayJob()');
    if (response.ok && (await response.json()).ok === false) {
      assert.equal(h.storage.activeRelayJob, undefined);
      assert.equal(h.messages.at(-1).ok, false);
    } else {
      assert.equal(h.storage.activeRelayJob.completion.response, 'do not lose me');
      assert.equal(h.messages.length, 0);
    }
  }
}

async function failedErrorDeliverySurvivesRestart() {
  const h = harness({activeRelayJob:job});
  h.ctx.fetch = async () => {throw Error('offline');};
  await h.run('failActiveRelayJob(' + JSON.stringify(job) + ', "page closed")');
  assert.equal(h.storage.activeRelayJob.completion.error, 'page closed');
  const restored = harness(h.storage);
  await restored.run('pollActiveRelayJob()');
  assert.equal(restored.storage.activeRelayJob, undefined);
  assert.equal(restored.messages.at(-1).ok, false);
}

async function backgroundProvidersAndRestart() {
  const pollNames = {chatgpt:'_chatgptPollTurnFn', claude:'_claudePollTurnFn',
                    gemini:'_domPollTurnFn', deepseek:'_deepseekPollTurnFn', venice:'_venicePollTurnFn'};
  for (const [provider, pollName] of Object.entries(pollNames)) {
    const h = harness({activeRelayJob:{...job,provider}});
    h.ctx.chrome.tabs.get = async id=>({id,active:false,autoDiscardable:true});
    let polls = 0;
    h.ctx.sendBusyHeartbeat = async () => true;
    h.ctx.ensurePageRuntime = async (id,p) => {assert.equal(id,7); assert.equal(p,provider);};
    h.ctx.chrome.scripting = {executeScript: async opts => {
      assert.equal(opts.target.tabId, 7);
      assert.equal(opts.func.name, pollName);
      polls++;
      return [{result:{text:'partial', thinking:'reasoning', done:false}}];
    }};
    await h.run('restoreActiveRelayJob()');
    await Promise.all(Array.from({length:20},()=>h.run('pollActiveRelayJob()')));
    assert.equal(polls,1, 'overlapping alarm/port/timer wakes must poll once');
    assert.equal(h.storage.activeRelayJob.sentText,'partial');
    assert.ok(h.updates.some(u=>u.autoDiscardable === false));
    const restored = harness(h.storage);
    restored.ctx.sendBusyHeartbeat = async () => true;
    restored.ctx.ensurePageRuntime = h.ctx.ensurePageRuntime;
    restored.ctx.resolveFinalChatUrl = async () => '';
    restored.ctx.chrome.scripting = {executeScript:async opts=>{
      assert.equal(opts.func.name,pollName);
      return [{result:{text:'finished while hidden',done:true}}];
    }};
    await restored.run('restoreActiveRelayJob()');
    await restored.run('pollActiveRelayJob()');
    assert.equal(restored.storage.activeRelayJob,undefined);
    assert.equal(restored.messages.at(-1).preview,'finished while hidden');
    assert.ok([...h.updates,...restored.updates].every(u=>u.active !== true));
    console.log('PASS inactive tab + restart',provider);
  }
}

async function submissionOwnsJobBeforeAwait() {
  for (const [provider, missingResult] of ['chatgpt','claude','gemini','deepseek','venice'].flatMap(p=>[[p,false],[p,true]])) {
    const h = harness();
    const gate = deferred();
    let submissions = 0, idleFetches = 0;
    const tab = {id:7,active:false,autoDiscardable:false,url:'https://example.test/'};
    h.ctx.scanOpenSiteStates = async()=>({ident:{clientId:'test'},hasAnyAiTab:true,
      tabsByProvider:{[provider]:[tab]},bestByProvider:{[provider]:tab}});
    h.ctx.fetch = async url => {
      assert.ok(url.includes('/pending-prompt?'));
      idleFetches++;
      return {ok:true,headers:{get:()=>null},json:async()=>({id:'start',prompt:'test',provider})};
    };
    h.ctx.ensureAliveAiTab = async()=>{await gate.promise;return {tab,recovered:false};};
    h.ctx.protectAiTabs = async()=>{};
    h.ctx.readLiveTabIdentity = async()=>({});
    h.ctx.ensurePageRuntime = async()=>true;
    h.ctx.chrome.scripting = {executeScript:async()=>{
      assert.equal(h.storage.activeRelayJob.phase,'starting');
      assert.equal(h.storage.activeRelayJob.tabId,7);
      await h.run('pollActiveRelayJob()');
      assert.equal(h.storage.activeRelayJob.phase,'starting');
      submissions++;
      return missingResult ? [] : [{result:{ok:true}}];
    }};
    const starting = h.run('checkAndRelay()');
    await new Promise(setImmediate);
    assert.equal(h.storage.activeRelayJob.phase,'preparing');
    await Promise.all(Array.from({length:20},()=>h.run('checkAndRelay()')));
    await h.run('pollActiveRelayJob()');
    assert.equal(h.storage.activeRelayJob.phase,'preparing');
    assert.equal(idleFetches,1);
    gate.resolve();
    await starting;
    assert.equal(submissions,1);
    assert.equal(h.storage.activeRelayJob.phase,missingResult ? 'starting' : 'running');
    assert.equal(h.run('injecting'),true);
    if (missingResult) {
      h.ctx.sendBusyHeartbeat=async()=>true;
      h.ctx.resolveFinalChatUrl=async()=>'';
      h.ctx.chrome.scripting.executeScript=async()=>[{result:{text:'recovered answer',done:true}}];
      h.ctx.fetch=async()=>({ok:true,json:async()=>({ok:true})});
      await h.run('pollActiveRelayJob()');
      assert.equal(h.messages.at(-1).preview,'recovered answer');
      assert.equal(submissions,1);
    }
    console.log('PASS startup ownership',provider);
  }
}

async function restartDuringPreparationReportsFailure() {
  const h = harness({activeRelayJob:{...job,tabId:null,phase:'preparing'}});
  await h.run('restoreActiveRelayJob()');
  await h.run('pollActiveRelayJob()');
  assert.equal(h.storage.activeRelayJob,undefined);
  assert.match(h.messages.at(-1).error,/restarted before prompt submission/);
}

async function longRunningJobIsNotSilentlyDropped() {
  const h = harness({activeRelayJob:{...job, startedAt:Date.now()-700000}});
  assert.equal(await h.run('restoreActiveRelayJob()'),true);
  assert.equal(h.storage.activeRelayJob.reqId,job.reqId);
}

async function staleHeartbeatCannotEraseCompletion() {
  const h = harness({activeRelayJob:job});
  const response = deferred();
  h.ctx.scanOpenSiteStates = async()=>({ident:{clientId:'test'}});
  h.ctx.fetch = async()=>{await response.promise;return {ok:true,headers:{get:()=> '0'},json:async()=>({})};};
  const heartbeat = h.run('sendBusyHeartbeat()');
  await new Promise(setImmediate);
  h.storage.activeRelayJob.completion = {response:'finished'};
  response.resolve();
  await heartbeat;
  assert.equal(h.storage.activeRelayJob.completion.response,'finished');
}

async function alarmRecoversWithoutTabKeepalive() {
  for (const provider of ['chatgpt','claude','gemini','deepseek','venice']) {
    const h = harness({activeRelayJob:{...job,provider}},true);
    h.ctx.sendBusyHeartbeat = async()=>true;
    h.ctx.ensurePageRuntime = async()=>true;
    h.ctx.resolveFinalChatUrl = async()=>'';
    h.ctx.chrome.scripting = {executeScript:async()=>[{result:{text:'alarm recovered',done:true}}]};
    // Do not execute ANY page timer, port message, or fast-loop timer.
    await new Promise(setImmediate);
    assert.equal(typeof h.listeners.alarm,'function');
    await h.listeners.alarm({name:'dom-relay-poll'});
    assert.equal(h.storage.activeRelayJob,undefined);
    assert.equal(h.messages.at(-1).preview,'alarm recovered');
    assert.ok(h.updates.every(u=>u.active !== true));
  }
}

async function visibilitySwitchWakesWorker() {
  const events = {};
  let sent = 0;
  const context = vm.createContext({
    console, Date, location:{href:'https://chatgpt.com/'}, navigator:{userAgent:'Chrome'},
    setInterval(){},
    window:{addEventListener(){}},
    document:{visibilityState:'hidden',addEventListener:(name,fn)=>{events[name]=fn;}},
    chrome:{runtime:{
      sendMessage(){sent++;},
      connect:()=>({onDisconnect:{addListener(){}},postMessage(){}}),
    }},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../wsa-browser-bridge/content_script_bridge.js'),'utf8'),context);
  await new Promise(setImmediate);
  const before = sent;
  events.visibilitychange();
  await new Promise(setImmediate);
  assert.equal(sent,before+1,'switching away must wake the worker, without waiting for a page timer');
}

async function interruptedSubmissionWaitsForPageTurn() {
  const h = harness({activeRelayJob:{...job,phase:'starting',submissionStartedAt:Date.now()}});
  h.ctx.sendBusyHeartbeat = async()=>true;
  h.ctx.ensurePageRuntime = async()=>true;
  h.ctx.chrome.scripting = {executeScript:async()=>[{result:{missing:true}}]};
  for (let i=0;i<20;i++) await h.run('pollActiveRelayJob()');
  assert.equal(h.storage.activeRelayJob.reqId,job.reqId);
  assert.equal(h.messages.length,0);
}

async function storageFailureDoesNotExposeIdleOwnership() {
  const h = harness();
  let scans = 0;
  h.ctx.scanOpenSiteStates = async()=>{scans++;};
  h.ctx.chrome.storage.local.get = async()=>{throw Error('storage unavailable');};
  await assert.rejects(h.run('checkAndRelay()'),/storage unavailable/);
  assert.equal(scans,0,'must not claim another request if saved ownership cannot be read');
}

async function resultSurvivesFailedStorageRemoval() {
  const h = harness({activeRelayJob:{...job,completion:{id:job.reqId,response:'saved'}}});
  const remove = h.ctx.chrome.storage.local.remove;
  h.ctx.chrome.storage.local.remove = async()=>{throw Error('storage busy');};
  await h.run('pollActiveRelayJob()');
  assert.equal(h.storage.activeRelayJob.completion.response,'saved');
  assert.equal(h.run('injecting'),true);
  h.ctx.chrome.storage.local.remove = remove;
  await h.run('pollActiveRelayJob()');
  assert.equal(h.storage.activeRelayJob,undefined);
}

async function heartbeatDoesNotWaitForPageScan() {
  const h = harness({activeRelayJob:job});
  h.ctx.scanOpenSiteStates = async()=>{throw Error('frozen page must not be probed');};
  let sent = false;
  h.ctx.fetch = async url=>{
    const qs = new URL(url).searchParams;
    assert.equal(qs.get('client_id'), job.clientId);
    assert.equal(qs.get('browser'), 'brave');
    assert.equal(qs.get('busy_req_id'), job.reqId);
    assert.equal(qs.get('busy'), '1');
    sent = true;
    return {ok:true,headers:{get:()=>null},json:async()=>null};
  };
  await h.run('sendBusyHeartbeat()');
  assert.equal(sent,true);
}

async function alarmHeartbeatsWhilePagePollIsBlocked() {
  const h = harness({activeRelayJob:job},true);
  let beats=0;
  h.ctx.sendBusyHeartbeat=async()=>{beats++;return true;};
  await new Promise(setImmediate);
  h.run('pollInFlight=true; injecting=true');
  await h.listeners.alarm({name:'dom-relay-poll'});
  assert.equal(beats,1);
  assert.equal(h.storage.activeRelayJob.reqId,job.reqId);
}

async function cancelledPollCannotResurrectOrOverwriteRequest() {
  for (const outcome of ['progress','done','throw']) {
    for (const replacement of [null,{...job,reqId:'new-job',sessionId:'other-project'}]) {
      const h=harness({activeRelayJob:job});
      const gate=deferred();
      h.ctx.sendBusyHeartbeat=async()=>true;
      h.ctx.ensurePageRuntime=async()=>true;
      h.ctx.chrome.scripting={executeScript:async()=>gate.promise};
      const polling=h.run('pollActiveRelayJob()');
      await new Promise(setImmediate);
      await h.run('clearLocalRelayJobState(true)');
      if (replacement) h.storage.activeRelayJob=structuredClone(replacement);
      if (outcome==='throw') gate.reject(Error('old page failed'));
      else gate.resolve([{result:{text:'late old answer',done:outcome==='done'}}]);
      await polling;
      assert.deepEqual(h.storage.activeRelayJob,replacement || undefined);
      assert.equal(h.messages.length,0);
    }
  }
}

async function delayedCompletionCannotOverwriteNewSession() {
  const h=harness({activeRelayJob:{...job,sessionId:'same-session'}});
  const gate=deferred();
  let sessionsSaved=0;
  h.ctx.resolveFinalChatUrl=async()=>gate.promise;
  h.ctx.saveRelaySession=async()=>{sessionsSaved++;};
  const completing=h.run('completeActiveRelayJob('+JSON.stringify({...job,sessionId:'same-session'})+', {text:"old",done:true})');
  await new Promise(setImmediate);
  await h.run('clearLocalRelayJobState(true)');
  h.storage.activeRelayJob={...job,reqId:'new-job'};
  gate.resolve('https://chatgpt.com/c/old');
  await completing;
  assert.equal(sessionsSaved,0);
  assert.equal(h.storage.activeRelayJob.reqId,'new-job');
}

async function delayedAcknowledgementCannotClearNewRequest() {
  const pending={...job,completion:{id:job.reqId,response:'old'}};
  const h=harness({activeRelayJob:pending});
  const gate=deferred();
  h.ctx.fetch=async()=>{await gate.promise;return {ok:true,json:async()=>({ok:true})};};
  const delivery=h.run('deliverActiveRelayResult('+JSON.stringify(pending)+')');
  await new Promise(setImmediate);
  await h.run('clearLocalRelayJobState(true)');
  h.storage.activeRelayJob={...job,reqId:'new-job'};
  h.run('injecting=true');
  gate.resolve();
  await delivery;
  assert.equal(h.storage.activeRelayJob.reqId,'new-job');
  assert.equal(h.run('injecting'),true);
  assert.equal(h.messages.length,0);
}

async function cancelledMutationCannotRaceStorageWrite() {
  const h=harness({activeRelayJob:job});
  const gate=deferred();
  const originalSet=h.ctx.chrome.storage.local.set;
  h.ctx.chrome.storage.local.set=async value=>{await gate.promise;return originalSet(value);};
  const updating=h.run('setStoredActiveJob('+JSON.stringify({...job,sentText:'partial'})+')');
  await new Promise(setImmediate);
  const cancelling=h.run('clearLocalRelayJobState(true,"test-job")');
  const claiming=h.run('setStoredActiveJob('+JSON.stringify({...job,reqId:'new-job'})+',true)');
  gate.resolve();
  assert.equal(await updating,true);
  assert.equal(await cancelling,true);
  assert.equal(await claiming,true);
  assert.equal(h.storage.activeRelayJob.reqId,'new-job');
  assert.equal(await h.run('setStoredActiveJob('+JSON.stringify(job)+')'),false);
}

async function unavailableImageTransferFailsForEveryRuntime() {
  const {run} = harness();
  for (const name of ['attachImagesToPage', 'attachImagesToClaude', 'attachImagesToEditor']) {
    const start = source.indexOf('  async function ' + name + '(');
    assert.ok(start >= 0);
    const definition = source.slice(start).match(/^[\s\S]*?\r?\n  }(?=\r?\n)/);
    assert.ok(definition);
    const attach = run('(' + definition[0] + ')');
    assert.equal((await attach([], null)).ok, true);
    const unavailable = await attach([{filename: 'screen.png', data: 'YQ=='}], null);
    assert.equal(unavailable.ok, false);
    assert.equal(unavailable.verified, false);
    assert.equal(unavailable.expected, 1);
  }
}

async function unchangedProgressRetriesAfterLostPost() {
  const h = harness({activeRelayJob: job});
  h.ctx.sendBusyHeartbeat = async () => true;
  h.ctx.ensurePageRuntime = async () => true;
  h.ctx.chrome.scripting = {executeScript: async () => [{result: {text: 'Inspecting layout', done: false}}]};
  const sent = [];
  h.ctx.fetch = async (url, options) => {
    assert.ok(url.endsWith('/pending-progress'));
    sent.push(JSON.parse(options.body));
    if (sent.length === 1) throw Error('lost post');
    return {ok: true, json: async () => ({ok: true})};
  };
  await h.run('pollActiveRelayJob()');
  assert.equal(h.storage.activeRelayJob.sentText, undefined);
  await h.run('pollActiveRelayJob()');
  await h.run('pollActiveRelayJob()');
  assert.deepEqual(sent.map(x => x.response), ['Inspecting layout', 'Inspecting layout']);
  assert.equal(h.storage.activeRelayJob.sentText, 'Inspecting layout');
}

async function submittedStageReportsVerifiedUploadAndTimings() {
  for (const [imagesAttached, uploadMs] of [[true, 380], [false, null]]) {
    const h = harness();
    const tab = {id: 11, active: false, autoDiscardable: false, url: 'https://gemini.google.com/app/test'};
    h.ctx.scanOpenSiteStates = async () => ({
      ident: {clientId: 'test'},
      hasAnyAiTab: true,
      tabsByProvider: {gemini: [tab]},
      bestByProvider: {gemini: tab},
    });
    const progressPosts = [];
    h.ctx.fetch = async (url, opts) => {
      if (url.includes('/pending-prompt?')) {
        return {
          ok: true,
          headers: {get: () => null},
          json: async () => ({
            id: 'req-vis-1',
            prompt: 'Inspect screenshot',
            provider: 'gemini',
            images: [{filename: 'sheet.png', data: 'YQ=='}],
          }),
        };
      }
      if (url.endsWith('/pending-progress')) {
        progressPosts.push(JSON.parse(opts.body));
        return {ok: true, json: async () => ({ok: true})};
      }
      return {ok: true, json: async () => ({ok: true})};
    };
    h.ctx.ensureAliveAiTab = async () => ({tab, recovered: false});
    h.ctx.protectAiTabs = async () => {};
    h.ctx.readLiveTabIdentity = async () => ({});
    h.ctx.ensurePageRuntime = async () => true;
    h.ctx.chrome.scripting = {
      executeScript: async () => [{
        result: {
          ok: true,
          images_attached: imagesAttached,
          upload_ms: uploadMs,
          submit_ms: 420,
        },
      }],
    };
    await h.run('checkAndRelay()');
    await new Promise(setImmediate);
    const sub = progressPosts.find(p => p.stage === 'submitted');
    assert.ok(sub, 'expected /pending-progress stage=submitted post');
    assert.equal(sub.id, 'req-vis-1');
    assert.equal(sub.seq, 0);
    assert.equal(sub.images_attached, imagesAttached);
    assert.equal(sub.submit_ms, 420);
    if (uploadMs !== null) {
      assert.equal(sub.upload_ms, uploadMs);
    } else {
      assert.equal(sub.upload_ms, undefined);
    }
  }
}

(async()=>{
  for (const test of [unavailableImageTransferFailsForEveryRuntime, unchangedProgressRetriesAfterLostPost, submittedStageReportsVerifiedUploadAndTimings, acquisitionIsSingleFlight, completionWaitsForAcknowledgement, failedDeliverySurvivesRestart,
    rejectedAndInvalidAcknowledgements, failedErrorDeliverySurvivesRestart, backgroundProvidersAndRestart,
    submissionOwnsJobBeforeAwait, restartDuringPreparationReportsFailure, longRunningJobIsNotSilentlyDropped,
    staleHeartbeatCannotEraseCompletion, alarmRecoversWithoutTabKeepalive, visibilitySwitchWakesWorker,
    interruptedSubmissionWaitsForPageTurn, storageFailureDoesNotExposeIdleOwnership,
    resultSurvivesFailedStorageRemoval, heartbeatDoesNotWaitForPageScan,
    alarmHeartbeatsWhilePagePollIsBlocked, cancelledPollCannotResurrectOrOverwriteRequest,
    delayedCompletionCannotOverwriteNewSession, delayedAcknowledgementCannotClearNewRequest,
    cancelledMutationCannotRaceStorageWrite]) {
    await test(); console.log('PASS', test.name);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
