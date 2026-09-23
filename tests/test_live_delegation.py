"""Real OS enforcement and installed-runtime protocol checks against offline fixtures.

Set DEEPSEEK_TEAM_REQUIRE_LIVE=1 in the dedicated Ubuntu CI job: missing bwrap,
namespace support, Codex or Claude is then a FAILURE, not a successful skip.
No public inference endpoint or real API credential is used by these tests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_deepseek_team import activation, coordination, managed, relay, sandbox, settings, worker, workspace
from codex_deepseek_team.routing import RoutingService

DRIVER = r'''#!/usr/bin/env python3
import errno,json,os,pathlib,socket,subprocess,sys
if '--help' in sys.argv:
    print('--strict-config --sandbox --ephemeral --json --ignore-rules danger-full-access '
          '--bare --tools --allowedTools --disallowedTools --permission-mode dontAsk '
          '--disable-slash-commands --setting-sources --strict-mcp-config --mcp-config '
          '--no-session-persistence --output-format')
    raise SystemExit(0)
task=json.loads(sys.stdin.read())
root=pathlib.Path.cwd()
# These are actual OS operations from an untrusted process, not prompt checks.
for name in ('.git/index', '.git/config', '.git/HEAD'):
    try:
        with open(root/name,'ab') as f: f.write(b'FORBIDDEN')
    except OSError: pass
    else: raise AssertionError('Git metadata writable')
assert not pathlib.Path(task['original']).exists(), 'original checkout visible'
assert not pathlib.Path(task['sibling']).exists(), 'another worker visible'
assert 'actual-host-credential' not in os.environ.values()
s=socket.socket(); s.settimeout(0.2)
try: s.connect(('1.1.1.1',443))
except OSError: pass
else: raise AssertionError('external network reachable')
finally: s.close()
if task['mode']=='readonly':
    before=(root/'calc.py').read_bytes()
    for name in ('calc.py','unexpected.py'):
        try: (root/name).write_text('FORBIDDEN')
        except OSError: pass
        else: raise AssertionError('read-only copy writable')
    assert (root/'calc.py').read_bytes()==before
    answer='Analysis: add subtracts instead of adding; READONLY_ENFORCED'
else:
    if task['mode']=='fix':
        (root/'calc.py').write_text('def add(a,b): return a+b\n')
        (root/'test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_sum(self): self.assertEqual(add(2,3),5)\n')
    elif task['mode']=='part':
        name=task['name']
        (root/(name+'.py')).write_text('VALUE='+repr(name)+'\n')
        (root/('test_'+name+'.py')).write_text('import unittest\nfrom '+name+' import VALUE\nclass TestPart(unittest.TestCase):\n def test_part(self): self.assertEqual(VALUE,'+repr(name)+')\n')
    elif task['mode']=='resume':
        assert (root/'partial.py').read_text()=='partial'
        (root/'partial.py').write_text('completed')
        (root/'test_partial.py').write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class TestPartial(unittest.TestCase):\n"
            " def test_completed(self): self.assertEqual(Path('partial.py').read_text(),'completed')\n")
    elif task['mode']=='fail':
        (root/'partial.py').write_text('partial')
        print(json.dumps({'type':'result','is_error':True,'result':'fixture task failed'}))
        raise SystemExit(9)
    elif task['mode']=='providerfail':
        (root/'provider_partial.py').write_text('provider partial')
        raise SystemExit(1)
    result=subprocess.run([sys.executable,'-m','unittest','discover','-q'],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    (root/'build').mkdir(exist_ok=True)
    (root/'build/check.txt').write_text('actual local checks passed')
    answer='IMPLEMENTED_AND_TESTED'
print(json.dumps({'type':'result','is_error':False,'result':answer}))
'''


class LiveBase(unittest.TestCase):
    def setUp(self):
        required = os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE') == '1'
        try:
            self.backend = sandbox.probe_backend()
        except sandbox.SandboxError as error:
            if required:
                self.fail('Required live namespace unavailable: ' + str(error))
            self.skipTest('Live OS check unavailable on this host: ' + str(error))
        self.tmp = tempfile.TemporaryDirectory(prefix='dst-live-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'original'
        self.source.mkdir()
        self.state = self.root / 'state'
        self.env_patch = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                               'DEEPSEEK_API_KEY': 'actual-host-credential',
                                               'DEEPSEEK_TEAM_STATE_DIR': str(self.state)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        (self.source / 'calc.py').write_text('def add(a,b): return a-b\n')
        (self.source / '.gitignore').write_text('build/\n__pycache__/\n')
        for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=test@example.test','commit','-qm','base')]:
            subprocess.run(['git','-C',str(self.source),*args],check=True,capture_output=True)
        self.driver = self.root / 'fixture-claude'
        self.driver.write_text(DRIVER)
        self.driver.chmod(0o700)

    def args(self, task, **changes):
        args = dict(task=task, os_sandbox='required', attempts=1, attempts_explicit=False,
                    runtime='claude', codex='codex', claude=str(self.driver),
                    state_dir=self.state, timeout=40, resume_after_failure=False,
                    coord_task=None, coord_assignment=None)
        args.update(changes)
        return SimpleNamespace(**args)

    def policy(self, level, access='auto'):
        return settings.resolve(self.source, delegation_level=level, access=access)

    def task(self, mode, sibling, **kwargs):
        return json.dumps(dict(mode=mode, original=str(self.source / 'calc.py'),
                               sibling=str(sibling.path / 'calc.py'), **kwargs))


class LiveDemoTests(LiveBase):
    def test_declared_checks_share_the_total_job_deadline(self):
        settings.set_values(self.source / settings.PROJECT_FILE, access='full-access')
        policy = settings.resolve(self.source)
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        task = coordination.open_task(self.source, session_id='deadline', turn_id='1',
            prompt='fix and verify', policy=policy, runtime='claude')
        features = dict(kind='implementation', domain='python', operation='fix',
            localization='known', coupling='local', verification='tests', clarity='clear',
            risk='low', scope_size='small', runtime='claude', model='deepseek-flash',
            effort='medium', context_version='deadline-v1')
        plan = coordination.plan_task(self.source, task['id'], dict(classification='substantial',
            deliverables=[dict(id='fix', kind='implementation', scope=['calc.py'],
                executor='auto', acceptance=['checks pass within timeout'], dependencies=[],
                checks=["python3 -c 'import time; time.sleep(3)'"], features=features)]))
        aid = plan['assignments'][0]['id']
        started = time.monotonic()
        result = managed.run(self.args(self.task('fix', sibling), timeout=2,
            coord_task=task['id'], coord_assignment=aid), policy, worker, copy)
        self.assertEqual(result, 124)
        self.assertLess(time.monotonic() - started, 4)
        row = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_kind'], 'environment')
        self.assertEqual(row['checks'][0]['exit_code'], 124)

    def test_preparation_revalidates_policy_and_head_before_credentials(self):
        original_probe = managed.development.probe
        for change in ('disabled', 'revoked', 'head'):
            with self.subTest(change=change):
                activation.set_enabled(self.source, True)
                settings.set_values(self.source / settings.PROJECT_FILE, access='full-access')
                policy = settings.resolve(self.source)
                copy = workspace.create(self.source, self.state)
                def probe(*args, **kwargs):
                    original_probe(*args, **kwargs)
                    if change == 'disabled':
                        activation.set_enabled(self.source, False)
                    elif change == 'revoked':
                        settings.set_values(self.source / settings.PROJECT_FILE, access='read-only')
                    else:
                        subprocess.run(['git', '-C', str(self.source), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.test', 'commit', '-qm', 'changed',
                            '--allow-empty'], check=True, capture_output=True)
                with patch.object(managed.development, 'probe', side_effect=probe), \
                     patch.object(worker, 'load_api_key', side_effect=AssertionError('stale credential access')):
                    with self.assertRaises(worker.WorkerError):
                        managed.run(self.args('bounded work'), policy, worker, copy)

    def _assert_post_start_preparation_failure_is_reconciled(self, failure_point):
        settings.set_values(
            self.source / '.deepseek-team.toml',
            delegation_level='auto', access='full-access')
        policy = self.policy('auto', 'full-access')
        service = RoutingService(self.source)
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        task = coordination.open_task(
            self.source, session_id='session-' + failure_point,
            turn_id='turn-' + failure_point, prompt='implement fix',
            policy=policy, runtime='claude')
        features = {
            'kind': 'implementation', 'domain': 'python', 'operation': 'fix',
            'localization': 'known', 'coupling': 'local', 'verification': 'tests',
            'clarity': 'clear', 'risk': 'low', 'scope_size': 'small',
            'runtime': 'claude', 'model': 'deepseek-flash', 'effort': 'medium',
            'context_version': 'default',
        }
        planned = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'impl', 'kind': 'implementation', 'scope': ['calc.py'],
                'executor': 'auto', 'acceptance': ['sum fixed'],
                'dependencies': [], 'checks': ['python3 -V'], 'features': features,
            }],
        })
        self.assertEqual(planned['deliverables'][0]['executor'], 'worker')
        decision_id = planned['deliverables'][0]['routing']['decision_id']
        decision = service.decision(decision_id)
        self.assertEqual(planned['assignments'][0]['status'], 'planned')
        aid = planned['assignments'][0]['id']
        args = self.args(self.task('fix', sibling), coord_task=task['id'], coord_assignment=aid)
        error = (workspace.WorkspaceError('simulated post-start preparation failure')
                 if failure_point == 'begin' else OSError('simulated workspace save failure'))
        with patch.object(copy, failure_point, side_effect=error):
            with self.assertRaises((worker.WorkerError, OSError)):
                managed.run(args, policy, worker, copy)

        assignment = coordination.load_task(self.source, task['id'])['assignments'][0]
        self.assertEqual(assignment['status'], 'failed')
        self.assertEqual(assignment['error_kind'], 'environment')
        self.assertEqual(assignment['workspace_id'], copy.id)
        self.assertEqual(assignment['routing_decision_id'], decision_id)
        self.assertTrue(assignment['routing_feedback'][0]['recorded'])
        observations = service.observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['outcome'], 'infrastructure')
        self.assertNotIn(observations[0]['outcome'], ('rework', 'rejected'))
        self.assertEqual(service.decision(decision_id), decision)
        self.assertEqual(service.status()['decisions'], 1)
        self.assertFalse(service.status()['admission']['active_cooldowns'])

    def test_copy_begin_failure_after_assignment_start_is_reconciled(self):
        self._assert_post_start_preparation_failure_is_reconciled('begin')

    def test_copy_save_failure_after_assignment_start_is_reconciled(self):
        self._assert_post_start_preparation_failure_is_reconciled('save')

    def test_four_profiles_with_real_permissions_and_local_tests(self):
        copies = [workspace.create(self.source, self.state) for _ in range(6)]
        source_index = (self.source / '.git/index').read_bytes()
        (self.source / 'calc.py').write_text('user uncommitted work\n')
        (self.source / 'private.env').write_text('not copied')
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            self.assertEqual(managed.run(self.args(self.task('readonly', copies[1])),
                                         self.policy(25), worker, copies[0]), 0)
            self.assertEqual(managed.run(self.args(self.task('fix', copies[0])),
                                         self.policy(50), worker, copies[1]), 0)
            self.assertTrue((copies[1].path / 'test_calc.py').exists())
            assignments = list(zip(copies[2:5], ('alpha','beta','gamma')))
            def execute(item):
                copy, name = item
                return managed.run(self.args(self.task('part', copies[1], name=name)),
                                   self.policy(75), worker, copy)
            with ThreadPoolExecutor(max_workers=3) as pool:
                self.assertEqual(list(pool.map(execute, assignments)), [0,0,0])
            self.assertEqual(managed.run(self.args(self.task('readonly', copies[1])),
                                         self.policy(75,'read-only'), worker, copies[5]), 0)
        # Coordinator integrates results rather than re-implementing the parts.
        integration = workspace.create(self.source, self.state)
        for copy, name in assignments:
            for filename in (name+'.py', 'test_'+name+'.py'):
                shutil.copyfile(copy.path / filename, integration.path / filename)
        result = subprocess.run([sys.executable,'-m','unittest','discover','-q'],
                                cwd=integration.path,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Ran 3 tests',result.stderr)
        self.assertEqual((self.source/'calc.py').read_text(),'user uncommitted work\n')
        self.assertEqual((self.source/'.git/index').read_bytes(),source_index)
        self.assertEqual((self.source/'private.env').read_text(),'not copied')
        for index in (0,5):
            self.assertFalse((copies[index].path/'unexpected.py').exists())
            self.assertEqual(workspace.load(self.state,copies[index].id).metadata['changed_files'],[])
        print('DEMO: 25/read-only PASS; 50/full-access fix+test PASS; '
              '75/full-access three parallel copies + integrated tests PASS; 75/read-only PASS')

    def test_partial_failure_requires_explicit_recovery_and_preserves_changes(self):
        copy, sibling = [workspace.create(self.source,self.state) for _ in range(2)]
        self.assertEqual(managed.run(self.args(self.task('fail',sibling)),self.policy(50),worker,copy),9)
        self.assertEqual((copy.path/'partial.py').read_text(),'partial')
        with self.assertRaises(worker.WorkerError):
            managed.run(self.args(self.task('resume',sibling)),self.policy(50),worker,copy)
        self.assertEqual((copy.path/'partial.py').read_text(),'partial')
        args=self.args(self.task('resume',sibling),resume_after_failure=True)
        self.assertEqual(managed.run(args,self.policy(50),worker,copy),0)
        self.assertEqual((copy.path/'partial.py').read_text(),'completed')
        self.assertTrue((copy.path/'test_partial.py').exists())
        # A successful dirty copy can be reused without recovery or reset.
        self.assertEqual(managed.run(self.args(self.task('fix',sibling)),self.policy(50),worker,copy),0)
        self.assertEqual((copy.path/'partial.py').read_text(),'completed')


    def test_managed_runner_automatically_records_assignment_checks_and_worker_delta(self):
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        policy = self.policy(50)
        task = coordination.open_task(
            self.source, session_id='session-runner', turn_id='turn-runner',
            prompt='implement fix', policy=policy)
        planned = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'impl', 'kind': 'implementation', 'scope': ['calc.py', 'test_calc.py'],
                'executor': 'worker', 'acceptance': ['sum fixed'],
                'dependencies': [{'kind': 'command', 'value': 'python3'}],
                'checks': ['python3 -m unittest discover -q'],
            }],
        })
        aid = planned['assignments'][0]['id']
        args = self.args(self.task('fix', sibling), coord_task=task['id'], coord_assignment=aid)
        self.assertEqual(managed.run(args, policy, worker, copy), 0)
        record = coordination.load_task(self.source, task['id'])
        assignment = record['assignments'][0]
        self.assertEqual(assignment['status'], 'succeeded')
        self.assertEqual(assignment['workspace_id'], copy.id)
        self.assertTrue(any(row['exit_code'] == 0 for row in assignment['checks']))
        self.assertIn('calc.py', assignment['worker_changes'])
        self.assertNotIn('calc.py', assignment['prepared_changes'])

    def test_host_only_dependency_is_rejected_by_sandbox_preflight_before_key_read(self):
        copy, sibling = [workspace.create(self.source, self.state) for _ in range(2)]
        policy = self.policy(50)
        hostbin = self.root / 'hostbin'
        hostbin.mkdir()
        tool = hostbin / 'host-only-jdk'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o700)
        task = coordination.open_task(
            self.source, session_id='session-deps', turn_id='turn-deps',
            prompt='run jvm work', policy=policy)
        planned = coordination.plan_task(self.source, task['id'], {
            'classification': 'substantial',
            'deliverables': [{
                'id': 'jvm', 'kind': 'implementation', 'scope': ['calc.py'],
                'executor': 'worker', 'acceptance': ['done'],
                'dependencies': [{'kind': 'command', 'value': 'host-only-jdk'}],
                'checks': [],
            }],
        })
        aid = planned['assignments'][0]['id']
        args = self.args(self.task('fix', sibling), coord_task=task['id'], coord_assignment=aid)
        path = str(hostbin) + os.pathsep + os.environ.get('PATH', '')
        with patch.dict(os.environ, {'PATH': path}), patch.object(worker, 'load_api_key') as key:
            with self.assertRaises(worker.WorkerError) as caught:
                managed.run(args, policy, worker, copy)
        key.assert_not_called()
        self.assertIn('sandbox', str(caught.exception).lower())
        record = coordination.load_task(self.source, task['id'])
        self.assertTrue(any(row['code'] == 'dependency_unavailable'
                            for row in record['constraints']))

    def test_provider_failure_is_distinguished_even_when_runtime_output_is_malformed(self):
        copy, sibling = [workspace.create(self.source,self.state) for _ in range(2)]
        class FailedRelay:
            def __init__(self,*_args,**_kwargs):
                self.failures=[503]
            def __enter__(self):
                return self
            def __exit__(self,*_args):
                return False
        with patch.object(relay,'ProviderRelay',FailedRelay):
            with self.assertRaises(worker.WorkerError):
                managed.run(self.args(self.task('providerfail',sibling)),
                            self.policy(50),worker,copy)
        record=workspace.load(self.state,copy.id).metadata
        self.assertEqual(record['status'],'failed')
        self.assertEqual(record['error_kind'],'provider')
        self.assertTrue((copy.path/'provider_partial.py').exists())


class InstalledRuntimeTests(LiveBase):
    def _binary(self,runtime):
        binary=shutil.which(runtime)
        if not binary:
            if os.environ.get('DEEPSEEK_TEAM_REQUIRE_LIVE')=='1':
                self.fail('Required installed runtime missing: '+runtime)
            self.skipTest(runtime+' is not installed')
        return binary

    def test_real_codex_full_access_tools(self):
        self._exercise('codex',True)

    def test_real_codex_readonly_tools(self):
        self._exercise('codex',False)

    def test_real_claude_full_access_tools(self):
        self._exercise('claude',True)

    def test_real_claude_readonly_tools(self):
        self._exercise('claude',False)

    def _exercise(self,runtime,writable):
        binary=self._binary(runtime)
        copy=workspace.create(self.source,self.state)
        calls=[]; tool_results=[]; step=0
        # Read data must actually return via a tool, not via the final fixture text.
        evidence='EVIDENCE_'+secrets.token_hex(8)
        (copy.path/'evidence.txt').write_text(evidence)
        write_code = "from pathlib import Path; Path('unlisted.py').write_text('VALUE=42\\n')"
        test_code = 'from unlisted import VALUE; assert VALUE == 42; print("LOCAL_TEST_PASSED")'
        shell = 'cat evidence.txt; python3 -c ' + shlex.quote(write_code) + '; python3 -c ' + shlex.quote(test_code)
        if not writable:
            shell='cat evidence.txt; touch forbidden.txt'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                nonlocal step
                raw=self.rfile.read(int(self.headers['Content-Length']))
                body=json.loads(raw)
                calls.append({'path':self.path,'auth':self.headers.get('Authorization')=='Bearer actual-host-credential'})
                if self.path.endswith('count_tokens'):
                    payload=json.dumps({'input_tokens':10}).encode(); content='application/json'
                elif runtime=='codex':
                    for item in body.get('input',[]):
                        if isinstance(item,dict) and item.get('type') in ('function_call_output','custom_tool_call_output'):
                            tool_results.append(str(item.get('output','')))
                    tools={t.get('name') for t in body.get('tools',[])}
                    if step==0:
                        step+=1
                        if 'exec_command' in tools: name='exec_command'; arguments={'cmd':shell,'max_output_tokens':500}
                        elif 'shell_command' in tools: name='shell_command'; arguments={'command':shell,'timeout_ms':10000}
                        else: name='shell'; arguments={'command':['sh','-c',shell],'timeout_ms':10000}
                        output=[{'type':'function_call','id':'fc_1','call_id':'call_1','name':name,
                                 'arguments':json.dumps(arguments),'status':'completed'}]
                    else:
                        output=[{'type':'message','id':'msg_1','role':'assistant','status':'completed',
                                 'content':[{'type':'output_text','text':'RUNTIME_FIXTURE_DONE','annotations':[]}]}]
                    response={'id':'resp_fixture','object':'response','model':worker.MODEL,'created_at':0,
                              'status':'completed','output':output,'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}}
                    events=[('response.created',{'response':dict(response,status='in_progress',output=[])})]
                    for item in output:
                        events += [('response.output_item.added',{'output_index':0,'item':item}),
                                   ('response.output_item.done',{'output_index':0,'item':item})]
                    events.append(('response.completed',{'response':response}))
                    payload=''.join(f'event: {k}\ndata: {json.dumps(dict(v,type=k,sequence_number=i))}\n\n'
                                    for i,(k,v) in enumerate(events)).encode(); content='text/event-stream'
                else:
                    for message in body.get('messages',[]):
                        for item in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                            if item.get('type')=='tool_result': tool_results.append(str(item.get('content','')))
                    if step==0:
                        step+=1
                        block=({'type':'tool_use','id':'tool_1','name':'Bash','input':{'command':shell,'timeout':10000}}
                               if writable else {'type':'tool_use','id':'tool_1','name':'Read',
                                                 'input':{'file_path':str(copy.path/'evidence.txt')}})
                        stop='tool_use'
                    else:
                        block={'type':'text','text':'RUNTIME_FIXTURE_DONE'}; stop='end_turn'
                    message={'id':'msg_fixture','type':'message','role':'assistant','model':'deepseek-flash',
                             'content':[block],'stop_reason':stop,'stop_sequence':None,
                             'usage':{'input_tokens':10,'output_tokens':10}}
                    if body.get('stream'):
                        initial=dict(message,content=[],stop_reason=None)
                        if block['type'] == 'tool_use':
                            start_block = dict(block, input={})
                            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                        else:
                            start_block = dict(block, text='')
                            delta = {'type': 'text_delta', 'text': block['text']}
                        events=[('message_start',{'message':initial}),
                                ('content_block_start',{'index':0,'content_block':start_block}),
                                ('content_block_delta',{'index':0,'delta':delta}),
                                ('content_block_stop',{'index':0}),
                                ('message_delta',{'delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':10}}),
                                ('message_stop',{})]
                        payload=''.join(f'event: {k}\ndata: {json.dumps(dict(v,type=k))}\n\n' for k,v in events).encode()
                        content='text/event-stream'
                    else:
                        payload=json.dumps(message).encode(); content='application/json'
                self.send_response(200); self.send_header('Content-Type',content)
                self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        original_relay=relay.ProviderRelay
        factory=lambda path,key: original_relay(path,key,upstream=('http','127.0.0.1',server.server_port))
        out,err=io.StringIO(),io.StringIO()
        raw_results=[]
        execute=worker.execute
        def capture(*args):
            result=execute(*args)
            raw_results.append(result)
            return result
        try:
            args=self.args('Exercise the provided file operation, then report.',runtime=runtime,
                           codex=binary if runtime=='codex' else 'codex',claude=binary if runtime=='claude' else 'claude',timeout=60)
            with patch.object(relay,'ProviderRelay',side_effect=factory), patch.object(worker,'execute',side_effect=capture), redirect_stdout(out),redirect_stderr(err):
                code=managed.run(args,self.policy(50 if writable else 25),worker,copy)
            self.assertEqual(code,0,err.getvalue() + repr(raw_results))
            self.assertIn('RUNTIME_FIXTURE_DONE',out.getvalue())
            self.assertGreaterEqual(len(calls),2,'No actual runtime tool roundtrip: '+err.getvalue())
            self.assertTrue(all(c['auth'] for c in calls))
            self.assertTrue(any(evidence in result for result in tool_results),str(tool_results))
            if writable:
                self.assertTrue((copy.path/'unlisted.py').exists())
                self.assertTrue(any('LOCAL_TEST_PASSED' in r for r in tool_results),str(tool_results))
            else:
                self.assertFalse((copy.path/'forbidden.txt').exists())
            copy.verify()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__=='__main__':
    unittest.main(verbosity=2)
