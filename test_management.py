"""Offline contract tests: synthetic API responses only; no generated source execution or live writes."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def load(kind):
    path=Path(__file__).resolve().parent/'tool.py'
    spec=importlib.util.spec_from_file_location('test_admin_'+kind,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod


class Fake:
    def __init__(self, row=None):
        self.row=copy.deepcopy(row or {})
        self.calls=[]
    async def request(self, method,path,body=None,params=None,text=False):
        self.calls.append((method,path,copy.deepcopy(body),params))
        if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
        if method=='GET':return copy.deepcopy(self.row)
        self.row.update(copy.deepcopy(body or {}));return copy.deepcopy(self.row)


async def approve(event):return True
async def deny(event):return False


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_activation_preview_and_preservation(self):
        for kind, extension_type in (('tools', None), ('filters', 'filter'), ('actions', 'action'), ('pipes', 'pipe')):
            for before, after in ((False, True), (True, False)):
                for changes in ({'is_active': after}, {'is_active': after, 'description': 'Updated'}, {'name': 'Renamed'}):
                    with self.subTest(kind=kind, before=before, changes=changes):
                        mod = load(kind)
                        row = {'id': 'fixture', 'name': 'Fixture', 'content': 'class Fixture: pass',
                               'meta': {'custom': 'keep'}, 'is_active': before, 'is_global': True}
                        if extension_type:
                            row['type'] = extension_type
                        class Extension(Fake):
                            async def request(self, method, path, body=None, params=None, text=False):
                                if path == '/api/v1/models/all': return []
                                if path.endswith('/toggle'):
                                    self.calls.append((method, path, body, params))
                                    self.row['is_active'] = not self.row['is_active']
                                    return copy.deepcopy(self.row)
                                return await super().request(method, path, body, params, text)
                        client = Extension(row)
                        events = []
                        async def confirm(event):
                            events.append(event)
                            self.assertTrue(all(c[0] == 'GET' for c in client.calls))
                            return True
                        manager = mod._Manager(kind, client, {'id': 'admin', 'role': 'admin'}, confirm)
                        saved = await manager.update('fixture', changes)
                        self.assertEqual(len(events), 1)
                        diff = events[0]['data']['message']
                        if 'is_active' in changes:
                            self.assertIn('-  "is_active": ' + json.dumps(before), diff)
                            self.assertIn('+  "is_active": ' + json.dumps(after), diff)
                        self.assertEqual(saved['is_active'], changes.get('is_active', before))
                        self.assertTrue(saved['is_global'])
                        self.assertEqual(saved['content'], row['content'])
                        self.assertEqual(saved['meta'], {'custom': 'keep', **({'description': 'Updated'} if 'description' in changes else {})})
                        self.assertEqual(saved['name'], changes.get('name', 'Fixture'))
                        for method, path, body, _ in client.calls:
                            if path.endswith('/update'):
                                self.assertNotIn('is_active', body)

    async def test_pipe_crud_and_preservation(self):
        mod=load('pipes')
        class Pipes(Fake):
            async def request(self,method,path,body=None,params=None,text=False):
                self.calls.append((method,path,copy.deepcopy(body),params))
                if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
                if path=='/api/v1/models/all':return []
                if path=='/api/v1/functions/list':return [copy.deepcopy(self.row)] if self.row else []
                if path.endswith('/create'):
                    self.row={**body,'type':'pipe','is_active':False,'is_global':False}
                elif path.endswith('/update'):self.row.update(body)
                elif path.endswith('/toggle'):self.row['is_active']=not self.row['is_active']
                elif method=='DELETE':self.row={};return True
                return copy.deepcopy(self.row)
        c=Pipes();m=mod._Manager('pipes',c,{'id':'admin','role':'admin'},approve)
        source='class Pipe:\n    async def pipe(self, body: dict):\n        return "synthetic"'
        saved=await m.create({'id':'fixture','name':'Fixture','content':source,'is_active':True})
        self.assertEqual(saved['type'],'pipe');self.assertTrue(saved['is_active']);self.assertFalse(saved['is_global'])
        c.row['meta']['custom']='preserved'
        saved=await m.update('fixture',{'description':'Updated','is_active':False})
        self.assertFalse(saved['is_active']);self.assertEqual(saved['content'],source)
        self.assertEqual(saved['meta'],{'custom':'preserved','description':'Updated'})
        self.assertTrue((await m.delete('fixture'))['deleted'])
        self.assertTrue(all('/functions/' in r[1] or r[1] in ('/api/v1/auths/','/api/v1/models/all') for r in c.calls))

    async def test_pipe_type_isolation_dependencies_and_discovery(self):
        mod=load('pipes')
        mod._source('class Pipe: pass','pipes')
        for source in ('class Filter: pass','class Pipe: pass\nclass Action: pass'):
            with self.assertRaises(mod._Failure):mod._source(source,'pipes')
        class Pipes(Fake):
            async def request(self,method,path,body=None,params=None,text=False):
                self.calls.append((method,path,body,params))
                if path=='/api/v1/functions/list':return [{'id':'p','type':'pipe'},{'id':'f','type':'filter'}]
                if path=='/api/v1/models/all':return [{'id':'a','base_model_id':'p'},{'id':'b','base_model_id':'p.child','is_active':False},{'id':'c','base_model_id':'prefix'}]
                if path=='/api/models/base':return {'data':[{'id':'p.child','name':'Child'},{'id':'prefix','name':'Other'}]}
                return copy.deepcopy(self.row)
        c=Pipes({'id':'p','type':'pipe','is_active':True});m=mod._Manager('pipes',c,{'id':'admin','role':'admin'},deny)
        self.assertEqual([r['id'] for r in (await m.listing())['items']],['p'])
        self.assertEqual([r['id'] for r in (await mod._extra(m,'pipe_models','p',{}))['items']],['p.child'])
        self.assertEqual([r['id'] for r in await m.pipe_dependents('p')],['a','b'])
        for action in (m.delete('p'),m.update('p',{'is_active':False})):
            with self.assertRaisesRegex(mod._Failure,'still used'):await action
        self.assertTrue(all(x[0]=='GET' for x in c.calls))
        c.row['type']='filter'
        with self.assertRaisesRegex(mod._Failure,'function type'):await m.get('p')

    async def test_pipe_source_confirmation_and_dependency_recheck(self):
        mod=load('pipes');c=Fake({'id':'p','type':'pipe','name':'P','content':'class Pipe: pass','meta':{}})
        m=mod._Manager('pipes',c,{'id':'admin','role':'admin'},deny)
        with self.assertRaisesRegex(mod._Failure,'cancelled'):await m.update('p',{'content':'class Pipe:\n    pass'})
        self.assertTrue(all(x[0]=='GET' for x in c.calls))
        count=0
        async def request(method,path,body=None,params=None):
            nonlocal count
            if path=='/api/v1/models/all':
                count+=1
                return [] if count==1 else [{'id':'new-preset','base_model_id':'p.child'}]
            if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
            self.assertEqual(method,'GET')
            return copy.deepcopy(c.row)
        c.request=request;m=mod._Manager('pipes',c,{'id':'admin','role':'admin'},approve)
        with self.assertRaisesRegex(mod._Failure,'still used'):await m.delete('p')

    async def test_configuration_schemas_serialize_through_authenticated_entrypoint(self):
        for kind in ('models','knowledge','tools','tool_servers','users','groups','skills','filters','actions','pipes','terminals'):
            with self.subTest(resource=kind):
                mod=load(kind)
                if mod._RESOURCE != kind:
                    continue  # Standalone packages contain only their own resource Tool.
                class Session(Fake):
                    async def close(self):pass
                session=Session()
                request=SimpleNamespace(state=SimpleNamespace(),headers={'authorization':'Bearer synthetic'},cookies={})
                with patch.object(mod,'_Client',return_value=session):
                    result=json.loads(await mod.Tools()._run('schema',request=request,user={'id':'admin','role':'admin'}))
                self.assertTrue(result['ok'],result)
                self.assertEqual(result['result']['fields'],list(mod._SCHEMAS[kind]))
                self.assertNotIn('warning',result)
                self.assertEqual(len(session.calls),1)
                if kind=='models':
                    self.assertIn('toolIds',result['result']['meta'])
                    self.assertIn('system',result['result']['params'])

    async def test_managed_resource_warning_survives_schema_fix(self):
        mod=load('models')
        if mod._RESOURCE != 'models':
            self.skipTest('Model-only public method')
        class Session(Fake):
            async def close(self):pass
        request=SimpleNamespace(state=SimpleNamespace(),headers={'authorization':'Bearer synthetic'},cookies={})
        for metadata,description,warn in [({'michael_managed':'preset'},'',True),(None,'[michael-managed]',True),({},'',False)]:
            with self.subTest(metadata=metadata,description=description):
                session=Session({'id':'model','meta':metadata,'description':description})
                with patch.object(mod,'_Client',return_value=session):
                    result=json.loads(await mod.Tools().get_model('model',__request__=request,__user__={'id':'admin','role':'admin'}))
                self.assertTrue(result['ok'],result)
                self.assertEqual('warning' in result,warn)

    def manager(self,kind,row=None,event=approve):
        mod=load(kind);client=Fake(row)
        return mod,mod._Manager(kind,client,{'id':'admin','role':'admin'},event),client

    async def test_nonadmin_and_spoofed_identity_fail_before_mutations(self):
        for kind in ('models','knowledge','tools','tool_servers','users','groups','skills','filters','actions','pipes','terminals'):
            mod=load(kind);tool=mod.Tools()
            request=SimpleNamespace(state=SimpleNamespace(),headers={'authorization':'Bearer synthetic'},cookies={})
            with patch.object(mod,'_Client') as client:
                out=json.loads(await tool._run('list',request=request,user={'id':'someone','role':'user'}))
                self.assertFalse(out['ok']);client.assert_not_called()
            class Session(Fake):
                async def close(self):pass
            session=Session()
            with patch.object(mod,'_Client',return_value=session):
                out=json.loads(await tool._run('list',request=request,user={'id':'someone','role':'admin'}))
                self.assertFalse(out['ok'])
                self.assertEqual(len(session.calls),1)

    async def test_model_patch_preserves_unrelated_configuration(self):
        row={'id':'preset','name':'Before','params':{'system':'Keep','model_controls':{'custom':{}}},'meta':{'voice':{'voice':'a'},'toolIds':['existing']},'base_model_id':'base','is_active':True,'access_grants':[{'permission':'read'}]}
        mod,m,c=self.manager('models',row)
        saved=await m.update('preset',{'name':'After','params':{'temperature':0.2}})
        self.assertEqual(saved['params']['system'],'Keep');self.assertEqual(saved['meta'],row['meta'])
        post=next(x for x in c.calls if x[0]=='POST')
        self.assertNotIn('access_grants',post[2]);self.assertEqual(post[2]['params']['model_controls'],{'custom':{}})

    async def test_deferred_fields_rejected(self):
        for kind,changes in [('models',{'meta':{'voice':{}}}),('models',{'params':{'model_controls':{}}}),('groups',{'permissions':{}}),('terminals',{'policy':{}}),('users',{'password':'synthetic'})]:
            mod,m,c=self.manager(kind)
            with self.assertRaises(mod._Failure):await m.update('one',changes)
            self.assertFalse(c.calls)

    async def test_invalid_numeric_parameter(self):
        mod,m,c=self.manager('models')
        for value in ('hot',float('nan'),True):
            with self.assertRaises(mod._Failure):m.validate({'params':{'temperature':value}})

    async def test_source_validation_does_not_execute_code(self):
        mod,m,c=self.manager('tools')
        mod._source('raise RuntimeError("must not execute")\nclass Tools: pass','tools')
        for source in ('class Action: pass','class Tools: pass\nclass Pipe: pass','"""requirements: unreviewed"""\nclass Tools: pass'):
            with self.assertRaises(mod._Failure):mod._source(source,'tools')

    async def test_workspace_function_discovery_uses_saved_specs(self):
        specs=[{'name':'lookup','parameters':{'type':'object','properties':{}}}]
        mod,m,c=self.manager('tools',{'id':'t','specs':specs})
        result=await mod._extra(m,'specs','t',{})
        self.assertEqual(result['specs'],specs)
        self.assertEqual(len(c.calls),1)
        self.assertEqual(c.calls[0][1],'/api/v1/tools/id/t')

    async def test_wrong_function_type_rejected(self):
        mod,m,c=self.manager('filters',{'id':'a','type':'action'})
        with self.assertRaises(mod._Failure):await m.get('a')
        self.assertEqual(len(c.calls),1)

    async def test_sensitive_confirmation_false_missing_and_error_dict_fail(self):
        for event in (deny,None):
            mod,m,c=self.manager('tools',{'id':'t','name':'T','content':'class Tools: pass','meta':{}},event)
            with self.assertRaises(mod._Failure):await m.update('t',{'content':'class Tools:\n    pass'})
            self.assertFalse(any(x[0]!='GET' for x in c.calls))
        async def error(event):return {'error':'disconnected'}
        mod,m,c=self.manager('tools',{},error)
        with self.assertRaises(mod._Failure):await m.approve('a',{}, {})

    async def test_stale_target_after_approval_fails(self):
        mod,m,c=self.manager('tools',{'id':'t','name':'T','content':'class Tools: pass','meta':{}})
        async def change(event):c.row['name']='Other';return True
        m.event=change
        with self.assertRaises(mod._Failure):await m.update('t',{'name':'New'})
        self.assertFalse(any(x[0]=='POST' for x in c.calls))

    async def test_group_update_preserves_hierarchy_permissions_and_defaults(self):
        row={'id':'g','name':'G','description':'','parent_group_id':'parent','permissions':{'chat':{'file_upload':True}},'data':{'config':{'default_models':['m']}}}
        mod,m,c=self.manager('groups',row)
        saved=await m.update('g',{'description':'new'})
        for key in ('parent_group_id','permissions','data'):self.assertEqual(saved[key],row[key])

    async def test_password_and_returned_token_never_in_result(self):
        mod=load('users');tool=mod.Tools()
        if not hasattr(tool, 'create_user'): self.skipTest('User-only public method')
        class Session(Fake):
            async def close(self):pass
            async def request(self,method,path,body=None,**kwargs):
                self.calls.append((method,path,copy.deepcopy(body),None))
                if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
                if path=='/api/v1/auths/add':return {'id':'new','token':'synthetic-return-token'}
                return {'id':'new','name':'New','email':'new@example.invalid','role':'user','password':'synthetic-password','token':'synthetic-return-token'}
        client=Session();request=SimpleNamespace(state=SimpleNamespace(),headers={'authorization':'Bearer test'},cookies={})
        with patch.object(mod,'_Client',return_value=client):
            out=await tool.create_user('New','new@example.invalid','synthetic-password',__request__=request,__user__={'id':'admin','role':'admin'})
        self.assertTrue(json.loads(out)['ok']);self.assertNotIn('synthetic-password',out);self.assertNotIn('synthetic-return-token',out)
        sent=next(x[2] for x in client.calls if x[1]=='/api/v1/auths/add');self.assertEqual(sent['password'],'synthetic-password')

    async def test_user_update_normalizes_email_before_preview_write_and_readback(self):
        for changes in ({'email': 'Alice@Example.com'}, {'email': 'Alice@Example.com', 'role': 'admin'}, {'name': 'Renamed'}):
            with self.subTest(changes=changes):
                mod = load('users')
                if not hasattr(mod.Tools(), 'update_user'):
                    self.skipTest('User-only public method')
                original = {'id': 'target', 'name': 'Alice', 'email': 'old@example.com', 'role': 'user', 'settings': {'keep': True}}
                class Session(Fake):
                    async def close(self): pass
                    async def request(self, method, path, body=None, params=None, text=False):
                        if method == 'POST' and path == '/api/v1/users/target/update':
                            self.calls.append((method, path, copy.deepcopy(body), params))
                            self.row.update({key: value.lower() if key == 'email' else value for key, value in body.items()})
                            return copy.deepcopy(self.row)
                        return await super().request(method, path, body, params, text)
                client = Session(original)
                events = []
                async def confirm(event):
                    events.append(event)
                    self.assertTrue(all(method == 'GET' for method, _, _, _ in client.calls))
                    return True
                request = SimpleNamespace(state=SimpleNamespace(), headers={'authorization': 'Bearer synthetic'}, cookies={})
                supplied = copy.deepcopy(changes)
                with patch.object(mod, '_Client', return_value=client):
                    output = json.loads(await mod.Tools().update_user('target', json.dumps(changes), __request__=request, __user__={'id': 'admin', 'role': 'admin'}, __event_call__=confirm))
                self.assertTrue(output['ok'], output)
                expected = {**changes, **({'email': 'alice@example.com'} if 'email' in changes else {})}
                self.assertEqual(client.row, {**original, **expected})
                self.assertEqual(output['result'], {key: client.row[key] for key in ('id', 'name', 'email', 'role')})
                self.assertEqual(changes, supplied)
                writes = [body for method, _, body, _ in client.calls if method == 'POST']
                self.assertEqual(writes, [expected])
                self.assertEqual(len(events), int('role' in changes))
                if events:
                    self.assertIn('alice@example.com', events[0]['data']['message'])
                    self.assertNotIn('Alice@Example.com', events[0]['data']['message'])

    async def test_user_creation_normalizes_email_before_confirmation(self):
        mod, manager, client = self.manager('users')
        events = []
        async def confirm(event):
            events.append(event)
            return True
        manager.event = confirm
        class Accounts(Fake):
            async def request(self, method, path, body=None, params=None, text=False):
                if path == '/api/v1/auths/add':
                    self.calls.append((method, path, copy.deepcopy(body), params))
                    self.row = {'id': 'new', **{key: value.lower() if key == 'email' else value for key, value in body.items() if key != 'password'}}
                    return copy.deepcopy(self.row)
                return await super().request(method, path, body, params, text)
        client = Accounts()
        manager.api = client.request
        data = {'name': 'Alice', 'email': 'Alice@Example.com', 'password': 'synthetic-password', 'role': 'admin'}
        before = copy.deepcopy(data)
        saved = await manager.create(data)
        self.assertEqual(saved['email'], 'alice@example.com')
        self.assertEqual(data, before)
        self.assertIn('alice@example.com', events[0]['data']['message'])
        self.assertNotIn('Alice@Example.com', events[0]['data']['message'])
        self.assertNotIn('synthetic-password', events[0]['data']['message'])
        writes = [body for method, _, body, _ in client.calls if method == 'POST']
        self.assertEqual(writes, [{**data, 'email': 'alice@example.com'}])

    async def test_self_role_change_blocked(self):
        mod,m,c=self.manager('users',{'id':'admin','name':'Admin','role':'admin'})
        with self.assertRaises(mod._Failure):await m.update('admin',{'role':'user'})
        self.assertFalse(any(x[0]=='POST' for x in c.calls))

    async def test_last_admin_blocked(self):
        mod,m,c=self.manager('users')
        async def api(method,path,body=None,params=None):return {'users':[{'id':'last','role':'admin'}],'total':1}
        m.api=api
        with self.assertRaises(mod._Failure):await m.user_guard('last',{'role':'admin'})

    async def test_skill_operations_use_current_version(self):
        mod,m,c=self.manager('skills',{'id':'s','name':'S','version_id':'v1','content':'instructions'})
        await m.update('s',{'operations':[{'op':'put','path':'references/a.md','content':'new'}]})
        post=next(x for x in c.calls if x[0]=='POST');self.assertEqual(post[2]['expected_version_id'],'v1')
        self.assertNotIn('content',post[2]);self.assertNotIn('data',post[2])

    async def test_skill_traversal_and_binary_operations_rejected(self):
        mod,m,c=self.manager('skills')
        for op in ({'op':'put','path':'../bad','content':'x'},{'op':'put','path':'x','content':'x','encoding':'base64'}):
            with self.assertRaises(mod._Failure):m.validate({'operations':[op]})

    async def test_connection_changes_preserve_other_connections_and_secrets(self):
        mod,m,c=self.manager('tool_servers')
        rows=[{'url':'https://example.invalid/mcp','path':'','type':'mcp','key':'secret-original','auth_type':'bearer','info':{'id':'one','name':'One'},'config':{'enable':True,'access_grants':[]}}, {'url':'https://other.invalid','info':{'id':'other'},'key':'other-secret'}]
        original=copy.deepcopy(rows)
        async def api(method,path,body=None,params=None):
            if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
            if method=='POST':rows[:]=copy.deepcopy(body['TOOL_SERVER_CONNECTIONS'])
            return {'TOOL_SERVER_CONNECTIONS':copy.deepcopy(rows)}
        m.api=api
        await m.update('one',{'info':{'name':'Renamed'}})
        self.assertEqual(rows[1],original[1]);self.assertEqual(rows[0]['key'],'secret-original')

    async def test_access_grant_preserves_unrelated_grants_and_is_idempotent(self):
        mod,m,c=self.manager('models')
        prior={'principal_type':'user','principal_id':'other','permission':'write'}
        row={'id':'m','access_grants':[prior]};writes=[]
        async def api(method,path,body=None,params=None):
            if path=='/api/v1/auths/':return {'id':'admin','role':'admin'}
            if path.startswith('/api/v1/groups/'):return {'id':'g'}
            if method=='POST':writes.append(body);row['access_grants']=body['access_grants']
            return copy.deepcopy(row)
        m.api=api
        await m.access('m','grant','group','g','read')
        self.assertIn(prior,row['access_grants'])
        await m.access('m','grant','group','g','read');self.assertEqual(len(writes),1)

    async def test_attach_requires_file_owner(self):
        mod,m,c=self.manager('knowledge')
        async def api(method,path,body=None,params=None):return {'id':'file','user_id':'other'} if '/files/' in path else {'id':'kb','meta':{}}
        m.api=api
        with self.assertRaises(mod._Failure):await mod._extra(m,'attach_file','kb',{'file_id':'file'})

    async def test_detach_checks_all_pages_and_preserves_retained_retrieval(self):
        for shared_hash in (False, True):
            with self.subTest(shared_hash=shared_hash):
                mod, manager, client = self.manager('knowledge')
                uploads = {
                    'f': {'id': 'f', 'user_id': 'admin', 'hash': 'selected-content'},
                    'other': {'id': 'other', 'hash': 'other-content'},
                    'retained': {'id': 'retained', 'hash': 'selected-content' if shared_hash else 'retained-content'},
                }
                members = list(uploads)
                vectors = {row['hash'] for row in uploads.values()}
                before_vectors = vectors.copy()
                calls = []
                async def api(method, path, body=None, params=None):
                    calls.append((method, path, copy.deepcopy(body), params))
                    if path == '/api/v1/auths/': return {'id': 'admin', 'role': 'admin'}
                    if path == '/api/v1/knowledge/kb': return {'id': 'kb', 'meta': {}}
                    if path == '/api/v1/files/f': return copy.deepcopy(uploads['f'])
                    if path.endswith('/files'):
                        self.assertEqual(set(params), {'page'})
                        start = params['page'] - 1
                        return {'items': [copy.deepcopy(uploads[fid]) for fid in members[start:start+1]], 'total': len(members)}
                    if path.endswith('/file/remove'):
                        self.assertEqual(method, 'POST')
                        self.assertEqual(body, {'file_id': 'f'})
                        self.assertEqual(params, {'delete_file': 'false'})
                        members.remove('f')
                        vectors.discard(uploads['f']['hash'])
                        return {'id': 'kb'}
                    self.fail((method, path))
                manager.api = api
                if shared_hash:
                    with self.assertRaisesRegex(mod._Failure, 'Duplicate content hash'):
                        await mod._extra(manager, 'detach_file', 'kb', {'file_id': 'f'})
                    self.assertEqual(members, ['f', 'other', 'retained'])
                    self.assertEqual(vectors, before_vectors)
                    self.assertTrue(all(method == 'GET' for method, _, _, _ in calls))
                    self.assertEqual([params['page'] for _, path, _, params in calls if path.endswith('/files')], [1, 2, 3])
                else:
                    result = await mod._extra(manager, 'detach_file', 'kb', {'file_id': 'f'})
                    self.assertTrue(result['membership_verified'])
                    self.assertEqual(members, ['other', 'retained'])
                    self.assertEqual(vectors, {'other-content', 'retained-content'})
                    self.assertEqual(sum(method == 'POST' for method, _, _, _ in calls), 1)
                self.assertIn('f', uploads)
                self.assertFalse(any(method == 'DELETE' for method, _, _, _ in calls))

    async def test_detach_refuses_incomplete_inventory_without_mutation(self):
        mod, manager, client = self.manager('knowledge')
        calls = []
        async def api(method, path, body=None, params=None):
            calls.append((method, path))
            self.assertEqual(method, 'GET')
            if path == '/api/v1/auths/': return {'id': 'admin', 'role': 'admin'}
            if path.endswith('/files'): return {'items': [], 'total': 2}
            if path == '/api/v1/files/f': return {'id': 'f', 'hash': 'content'}
            return {'id': 'kb', 'meta': {}}
        manager.api = api
        with self.assertRaisesRegex(mod._Failure, 'inventory incomplete'):
            await mod._extra(manager, 'detach_file', 'kb', {'file_id': 'f'})
        self.assertTrue(all(method == 'GET' for method, _ in calls))

    async def test_redaction_preserves_model_token_parameters(self):
        mod=load('models');safe=mod._safe({'max_tokens':512,'token':'secret','key':'secret','params':{'temperature':0.1}})
        self.assertEqual(safe['max_tokens'],512);self.assertEqual(safe['token'],'[REDACTED]');self.assertEqual(safe['key'],'[REDACTED]')

    async def test_http_error_body_not_exposed(self):
        mod=load('users');client=mod._Client('https://example.invalid','test')
        async def handler(request):return mod.httpx.Response(422,json={'detail':'synthetic-password'})
        await client.http.aclose();client.http=mod.httpx.AsyncClient(transport=mod.httpx.MockTransport(handler),base_url='https://example.invalid')
        with self.assertRaises(mod._Failure) as ctx:await client.request('POST','/api/v1/auths/add',{'password':'synthetic-password'})
        self.assertNotIn('synthetic-password',str(ctx.exception));await client.close()

    async def test_schema_marked_secret_valve_is_redacted_even_with_innocuous_name(self):
        mod,m,c=self.manager('tools')
        async def api(method,path,body=None,params=None):
            if path.endswith('/spec'):
                return {'properties':{'service_value':{'type':'string','writeOnly':True,'default':'secret-default'}}}
            if path.endswith('/valves'):return {'service_value':'secret-value'}
            return {'id':'t'}
        m.api=api
        result=await mod._extra(m,'valves','t',{})
        self.assertEqual(result['values']['service_value'],'[REDACTED]')
        self.assertNotIn('default',result['schema']['properties']['service_value'])
        with self.assertRaises(mod._Failure):
            await mod._extra(m,'update_valves','t',{'changes_json':'{"service_value":"replacement"}'})


if __name__=='__main__':unittest.main()
