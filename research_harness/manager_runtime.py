"""Fixed manager/root pipe protocol; no worker-selected command or public output."""
from .restart_boundary import RestartEpoch
from .result_publisher import ValidatedCanaryResult, SCHEMA
from .schema import ContractError, canonical, loads, fields

LIMIT=16384

def read_frame(stream):
    line=stream.readline(LIMIT+1)
    if not line or len(line.encode('utf-8'))>LIMIT or not line.endswith('\n'):
        raise ContractError('invalid supervisor frame')
    value=loads(line)
    if type(value) is not dict:raise ContractError('object frame required')
    return value


def write_frame(stream,value):
    if type(value) is not dict:raise ContractError('object frame required')
    encoded=canonical(value).decode()+'\n'
    if len(encoded.encode())>LIMIT:raise ContractError('oversized supervisor frame')
    stream.write(encoded);stream.flush()


class PipeRuntime:
    """Only the protected manager receives these private launcher pipes.

    This runtime runs the fixed literal CPU child, not a Codex/model process.
    The launcher must have independently verified its deployment manifest.
    """
    deployment_contract_verified=True

    def __init__(self,incoming,outgoing,epoch):
        if type(epoch) is not RestartEpoch:raise ContractError('launcher epoch required')
        self.incoming=incoming;self.outgoing=outgoing;self.epoch=epoch

    def current_epoch(self):return self.epoch

    def preflight(self):
        write_frame(self.outgoing,{'op':'preflight'})
        response=read_frame(self.incoming)
        if set(response)!={'ready'} or response['ready'] is not True:raise ContractError('supervisor admission failed')
        return self.epoch

    def run(self,binding,recheck):
        from .service_state import validate_binding
        validate_binding(binding)
        write_frame(self.outgoing,{'op':'fixed_cpu','binding':binding})
        while True:
            frame=read_frame(self.incoming)
            if frame=={'op':'recheck'}:
                try:recheck()
                except BaseException:
                    write_frame(self.outgoing,{'approved':False});raise
                write_frame(self.outgoing,{'approved':True})
                continue
            fields(frame,('result',))
            data=frame['result'];fields(data,('checksum','elapsed_seconds','model_called'))
            if data['model_called'] is not False:raise ContractError('unsupported model result')
            expected={key:binding[key] for key in ('request_id','run_id','issue_number','plan_digest','source_sha')}
            artifact=dict(schema=SCHEMA,**expected,outcome='success',checksum=data['checksum'],elapsed_seconds=data['elapsed_seconds'])
            return ValidatedCanaryResult.from_bytes(canonical(artifact),**expected)
