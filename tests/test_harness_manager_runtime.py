"""Private protocol tests use in-memory text streams, never a live root launcher."""
import io
import json
import unittest

from research_harness.manager_runtime import LIMIT, PipeRuntime, read_frame, write_frame
from research_harness.result_publisher import EXPECTED_CHECKSUM, ValidatedCanaryResult
from research_harness.schema import ContractError, canonical
from test_harness_restart_boundary import epoch

BINDING = dict(issue_number=70, issue_content_sha256='a' * 64, request_id='request-1',
               run_id='run-1', plan_digest='c' * 64, source_sha='b' * 40)


def encoded(value):
    return canonical(value).decode() + '\n'


def result_frame(**changes):
    return {'result': {**dict(checksum=EXPECTED_CHECKSUM, elapsed_seconds=.01, model_called=False), **changes}}


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
        self.assertEqual(outgoing.frames()[1:], [{'approved': False}])
        self.assertEqual(read_frame(incoming), result_frame())

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
