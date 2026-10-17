"""Private protocol tests use in-memory text streams, never a live root launcher."""
import io
import json
import subprocess
import unittest
from urllib.error import HTTPError, URLError
import weakref

from research_harness import manager_runtime
from research_harness.manager_github import ManagerGitHubRateLimited
from research_harness.manager_runtime import (LIMIT, PipeRuntime, RECHECK_REASONS,
    RECHECK_ORIGINS, RecheckRejected, read_frame, write_frame,
    safe_recheck_failure, require_recheck_reply)
from research_harness.owner_approval import MissingApproval, RevokedApproval
from research_harness.result_publisher import EXPECTED_CHECKSUM, ValidatedCanaryResult
from research_harness.schema import ContractError, canonical
from test_harness_restart_boundary import epoch

BINDING = dict(issue_number=70, issue_content_sha256='a' * 64, request_id='request-1',
               run_id='run-1', plan_digest='c' * 64, source_sha='b' * 40)


def encoded(value):
    return canonical(value).decode() + '\n'


def result_frame(**changes):
    return {'result': {**dict(checksum=EXPECTED_CHECKSUM, elapsed_seconds=.01, model_called=False), **changes}}


SECRET='private-token https://secret.invalid/body stderr=private'


class RecheckDiagnosticTests(unittest.TestCase):
    def test_all_fixed_reasons_and_origins_round_trip_and_return_fresh_records(self):
        self.assertIs(type(RECHECK_REASONS),frozenset)
        self.assertIs(type(RECHECK_ORIGINS),frozenset)
        for reason in RECHECK_REASONS:
            for origin in RECHECK_ORIGINS:
                with self.subTest(reason=reason,origin=origin):
                    exc=RecheckRejected(reason,origin=origin)
                    self.assertEqual(exc.args,('approval recheck rejected',))
                    expected={'reason':reason,'origin':origin}
                    actual=safe_recheck_failure(exc,origin='approval_callback')
                    self.assertEqual(actual,expected)
                    actual.update(reason=SECRET,origin=SECRET)
                    self.assertEqual(safe_recheck_failure(exc),expected)

    def test_invalid_constructor_enums_cannot_enter_exception_or_record(self):
        class String(str):pass
        for invalid in (SECRET,None,True,{},[],String('changed')):
            for reason,origin in ((invalid,'manager'),('expired',invalid)):
                exc=RecheckRejected(reason,origin=origin)
                self.assertEqual(exc.args,('approval recheck rejected',))
                self.assertEqual(safe_recheck_failure(exc),{'reason':'unknown','origin':'manager'})
                self.assertNotIn(SECRET,str(exc))

    def test_public_mutation_forged_instances_and_subclasses_are_not_authority(self):
        original=RecheckRejected('expired')
        original.args=(SECRET,)
        original.reason=original.origin=original._record=SECRET
        self.assertEqual(safe_recheck_failure(original),{'reason':'expired','origin':'manager'})
        forged=RecheckRejected.__new__(RecheckRejected)
        forged.reason='changed';forged.origin='manager';forged.args=(SECRET,)
        class Subclass(RecheckRejected):pass
        for exc in (forged,Subclass('changed')):
            self.assertEqual(safe_recheck_failure(exc,origin='approval_callback'),
                {'reason':'unknown','origin':'approval_callback'})
        other=RuntimeError(SECRET)
        other.reason='expired';other.origin='manager'
        self.assertEqual(safe_recheck_failure(other),{'reason':'unknown','origin':'manager'})

    def test_private_records_are_immutable_validated_and_weakly_held(self):
        class String(str):pass
        exc=RecheckRejected('changed')
        self.assertIs(type(manager_runtime._RECHECK_FAILURES[exc]),tuple)
        for record in (None,{},['changed','manager'],('changed',),
                (SECRET,'manager'),('changed',SECRET),(String('changed'),'manager')):
            manager_runtime._RECHECK_FAILURES[exc]=record
            self.assertEqual(safe_recheck_failure(exc),{'reason':'unknown','origin':'manager'})
        reference=weakref.ref(exc)
        del exc
        self.assertIsNone(reference())

    def test_source_failure_types_only_never_messages_headers_or_payloads(self):
        failures=[
            (HTTPError(SECRET,429,SECRET,{'secret':SECRET},None),'source_rate_limited'),
            (ManagerGitHubRateLimited(403),'source_rate_limited'),
            (HTTPError(SECRET,403,SECRET,{'X-RateLimit-Remaining':'0'},None),'source_unavailable'),
            (HTTPError(SECRET,500,SECRET,{},None),'source_unavailable'),
            (HTTPError(SECRET,'429',SECRET,{},None),'source_unavailable'),
            (TimeoutError(SECRET),'source_timeout'),
            (subprocess.TimeoutExpired(SECRET,1,output=SECRET,stderr=SECRET),'source_timeout'),
            (URLError(SECRET),'source_unavailable'),
            (URLError(TimeoutError(SECRET)),'source_unavailable'),
            (OSError(SECRET),'source_unavailable'),
            (subprocess.CalledProcessError(1,SECRET,output=SECRET,stderr=SECRET),'source_command_failed'),
            (MissingApproval(SECRET),'changed'),(RevokedApproval(SECRET),'changed'),
            (ContractError(SECRET),'contract'),(RuntimeError(SECRET),'unknown'),
            (RuntimeError('expired changed 429 rate limited timeout'),'unknown'),
            (KeyboardInterrupt(SECRET),'unknown'),
        ]
        for exc,reason in failures:
            with self.subTest(kind=type(exc).__name__,reason=reason):
                actual=safe_recheck_failure(exc,origin='approval_callback')
                self.assertEqual(actual,{'reason':reason,'origin':'approval_callback'})
                self.assertNotIn(SECRET,json.dumps(actual))
                self.assertEqual(safe_recheck_failure(exc,origin=SECRET),
                    {'reason':'unknown','origin':'manager'})

    def test_exception_chains_do_not_supply_a_reason(self):
        exc=RuntimeError(SECRET)
        exc.__cause__=RecheckRejected('expired')
        self.assertEqual(safe_recheck_failure(exc),{'reason':'unknown','origin':'manager'})

    def test_hostile_subclass_accessors_hashing_and_forged_dict_are_never_called(self):
        class HostileHTTP(HTTPError):
            def __init__(self,*args):
                object.__setattr__(self,'_hostile_ready',False)
                super().__init__(*args)
                object.__setattr__(self,'_hostile_ready',True)
            def __getattribute__(self,name):
                if (name in ('__dict__','code','__class__')
                        and object.__getattribute__(self,'_hostile_ready')):
                    raise AssertionError('must not read hostile attribute')
                return super().__getattribute__(name)
        class HostileDictionary(dict):
            def get(self,*args):raise AssertionError('must not call forged dict')
        class HostileRejected(RecheckRejected):
            def __hash__(self):raise AssertionError('must not hash subclass')
        class SpoofedError(RuntimeError):
            @property
            def __class__(self):return HTTPError
        forged=HTTPError(SECRET,429,SECRET,{},None)
        forged.__dict__=HostileDictionary(code=429)
        for exc in (HostileHTTP(SECRET,429,SECRET,{},None),forged,
                HostileRejected('expired'),SpoofedError(SECRET)):
            self.assertEqual(safe_recheck_failure(exc),{'reason':'unknown','origin':'manager'})


class RecheckReplyTests(unittest.TestCase):
    def test_true_frame_is_exact_and_negative_frames_preserve_fixed_diagnostics(self):
        self.assertIsNone(require_recheck_reply({'approved':True}))
        for reason in RECHECK_REASONS:
            for origin in RECHECK_ORIGINS:
                with self.assertRaises(RecheckRejected) as caught:
                    require_recheck_reply({'approved':False,'reason':reason,'origin':origin})
                self.assertEqual(safe_recheck_failure(caught.exception),{'reason':reason,'origin':origin})

    def test_old_boolean_denial_and_malformed_or_secret_fields_are_contract(self):
        class Mapping(dict):pass
        frames=[None,[],{},Mapping(approved=True),{'approved':1},{'approved':0},
            {'approved':False},{'approved':True,'reason':'changed','origin':'manager'},
            {'approved':False,'reason':'expired'},
            {'approved':False,'reason':SECRET,'origin':'manager'},
            {'approved':False,'reason':'changed','origin':SECRET},
            {'approved':False,'reason':{},'origin':'manager'},
            {'approved':False,'reason':'changed','origin':[]},
            {'approved':False,'reason':'changed','origin':'manager','stderr':SECRET},
            {'approved':True,'token':SECRET},
            {'approved':0,'reason':'changed','origin':'manager'},
        ]
        for frame in frames:
            with self.subTest(frame=frame),self.assertRaises(RecheckRejected) as caught:
                require_recheck_reply(frame)
            self.assertEqual(safe_recheck_failure(caught.exception),
                {'reason':'contract','origin':'launcher_ipc'})
            self.assertNotIn(SECRET,str(caught.exception))


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1

    def frames(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]


class FrameTests(unittest.TestCase):
    def test_one_bounded_object_frame_is_read_without_consuming_next_frame(self):
        incoming = io.StringIO(encoded({'ready': True}) + encoded({'next': 1}))
        self.assertEqual(read_frame(incoming), {'ready': True})
        self.assertEqual(read_frame(incoming), {'next': 1})

    def test_empty_partial_nonobject_duplicate_nonfinite_and_oversize_frames_rejected(self):
        for text in ('', '\n', '{"x":1}', '[]\n', 'null\n', '1\n',
                     '{"x":1,"x":2}\n', '{"x":NaN}\n', 'x' * (LIMIT + 1) + '\n'):
            with self.subTest(text=text[:50]), self.assertRaises(ContractError):
                read_frame(io.StringIO(text))

    def test_utf8_byte_limit_cannot_be_bypassed_with_multibyte_characters(self):
        text = json.dumps({'x': '界' * 6000}, ensure_ascii=False) + '\n'
        self.assertLess(len(text), LIMIT)
        self.assertGreater(len(text.encode()), LIMIT)
        with self.assertRaises(ContractError):
            read_frame(io.StringIO(text))

    def test_write_is_one_canonical_object_with_newline_and_flush(self):
        stream = Output()
        write_frame(stream, {'z': 1, 'a': True})
        self.assertEqual(stream.getvalue(), '{"a":true,"z":1}\n')
        self.assertEqual(stream.flushes, 1)

    def test_nonobject_nonfinite_or_oversize_write_has_no_partial_output(self):
        for value in ([], None, 1, 'text', {'x': float('nan')}, {'x': 'x' * LIMIT}):
            with self.subTest(value=str(value)[:50]):
                stream = Output()
                with self.assertRaises(ContractError):
                    write_frame(stream, value)
                self.assertEqual(stream.getvalue(), '')
                self.assertEqual(stream.flushes, 0)


class PipeRuntimeTests(unittest.TestCase):
    def runtime(self, frames):
        incoming = io.StringIO(''.join(encoded(frame) for frame in frames))
        outgoing = Output()
        return PipeRuntime(incoming, outgoing, epoch()), incoming, outgoing

    def test_prepare_requires_exact_boolean_true(self):
        for frame in ({'prepared':1},{'prepared':False},{'prepared':True,'extra':1}):
            runtime,_,_=self.runtime([frame])
            with self.assertRaises(ContractError):runtime.prepare_plan({'kind':'cpu-canary-v1'})

    def test_constructor_requires_typed_epoch(self):
        for value in (None, {}, epoch().to_dict()):
            with self.assertRaises(ContractError):
                PipeRuntime(io.StringIO(), Output(), value)

    def test_preflight_emits_fixed_operation_and_returns_bound_epoch(self):
        runtime, _, outgoing = self.runtime([{'ready': True}])
        self.assertEqual(runtime.preflight(), epoch())
        self.assertEqual(runtime.current_epoch(), epoch())
        self.assertEqual(outgoing.frames(), [{'op': 'preflight'}])

    def test_preflight_requires_exact_true_without_unknown_fields(self):
        for frame in ({'ready': 1}, {'ready': False}, {'ready': None}, {},
                      {'ready': True, 'command': 'untrusted'}, {'ready': 'true'}):
            runtime, _, outgoing = self.runtime([frame])
            with self.subTest(frame=frame), self.assertRaises(ContractError):
                runtime.preflight()
            self.assertEqual(outgoing.frames(), [{'op': 'preflight'}])

    def test_fixed_cpu_result_is_independently_rebound_and_validated(self):
        runtime, _, outgoing = self.runtime([result_frame()])
        result = runtime.run(BINDING, lambda: None)
        self.assertIs(type(result), ValidatedCanaryResult)
        self.assertEqual(result.checksum, EXPECTED_CHECKSUM)
        self.assertEqual(result.issue_number, BINDING['issue_number'])
        self.assertEqual(result.request_id, BINDING['request_id'])
        self.assertEqual(result.source_sha, BINDING['source_sha'])
        self.assertEqual(outgoing.frames(), [{'op': 'fixed_cpu', 'binding': BINDING}])

    def test_recheck_frames_require_current_approval_and_fixed_reply(self):
        runtime, _, outgoing = self.runtime([{'op': 'recheck'}, {'op': 'recheck'}, result_frame()])
        checks = []
        result = runtime.run(BINDING, lambda: checks.append('checked'))
        self.assertEqual(len(checks), 2)
        self.assertEqual(result.checksum, EXPECTED_CHECKSUM)
        self.assertEqual(outgoing.frames()[1:], [{'approved': True}, {'approved': True}])
        self.assertEqual(outgoing.flushes, 3)

    def test_recheck_revocation_returns_false_and_stops_before_result(self):
        runtime, incoming, outgoing = self.runtime([{'op': 'recheck'}, result_frame()])
        def deny():
            raise ContractError('synthetic approval revoked')
        with self.assertRaises(ContractError):
            runtime.run(BINDING, deny)
        self.assertEqual(outgoing.frames()[1:], [{'approved': False,'reason':'contract','origin':'manager'}])
        self.assertEqual(read_frame(incoming), result_frame())

    def test_recheck_denial_preserves_reason_and_original_exception_without_payload(self):
        failures=[RecheckRejected('expired'),RecheckRejected('refresh_stale'),
            HTTPError(SECRET,429,SECRET,{},None),TimeoutError(SECRET),
            ContractError(SECRET),RuntimeError(SECRET),KeyboardInterrupt(SECRET)]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__):
                runtime,incoming,outgoing=self.runtime([{'op':'recheck'},result_frame()])
                calls=[]
                def deny():
                    calls.append('called')
                    raise failure
                with self.assertRaises(type(failure)) as caught:
                    runtime.run(BINDING,deny)
                self.assertIs(caught.exception,failure)
                self.assertEqual(calls,['called'])
                self.assertEqual(outgoing.frames()[1:],[dict(approved=False,**safe_recheck_failure(failure))])
                self.assertEqual(read_frame(incoming),result_frame())
                self.assertNotIn(SECRET,outgoing.getvalue())

    def test_result_cannot_include_model_output_commands_or_unknown_data(self):
        frames = [{'op': 'recheck', 'command': 'bad'}, {'op': 'shell'},
                  {'result': result_frame()['result'], 'stdout': 'untrusted'},
                  {'result': {}}, {'result': []}, result_frame(model_called=True),
                  result_frame(model_called=0), result_frame(model_called=None),
                  result_frame(checksum=1), result_frame(checksum=True),
                  result_frame(elapsed_seconds=True), result_frame(elapsed_seconds=-1),
                  result_frame(elapsed_seconds=121), result_frame(model_text='private text')]
        for frame in frames:
            runtime, _, outgoing = self.runtime([frame])
            with self.subTest(frame=frame), self.assertRaises(ContractError):
                runtime.run(BINDING, lambda: None)
            self.assertEqual(outgoing.frames(), [{'op': 'fixed_cpu', 'binding': BINDING}])

    def test_eof_before_result_never_fabricates_success(self):
        runtime, _, outgoing = self.runtime([])
        with self.assertRaises(ContractError):
            runtime.run(BINDING, lambda: None)
        self.assertEqual(outgoing.frames(), [{'op': 'fixed_cpu', 'binding': BINDING}])


if __name__ == '__main__':
    unittest.main()
