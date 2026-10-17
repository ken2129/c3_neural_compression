"""Offline validation of host-collected Docker facts; never invokes Docker."""
import copy
import unittest

from research_harness.deployment_contract import validate_dedicated_deployment
from research_harness.schema import ContractError


CONTAINER_ID = 'a' * 64
SOURCE_SHA = 'b' * 40
VOLUME_NAME = 'dedicated-controller-state'
WORK_TMPFS = 'rw,noexec,nosuid,nodev,size=256m,mode=0700,uid=10002,gid=10002'


def volume_fixture():
    return {'Name': VOLUME_NAME, 'Driver': 'local', 'Scope': 'local', 'Options': None}


def inspect_fixture():
    return {
        'Id': CONTAINER_ID,
        'Config': {
            'Labels': {'c3.purpose': 'dedicated-research-controller',
                       'c3.source-sha': SOURCE_SHA},
            'Hostname': CONTAINER_ID[:12],
            'User': '0:0',
            'Entrypoint': ['python', '-I', '-B', '/opt/service/launcher.py'],
            'Cmd': None,
        },
        'HostConfig': {
            'Privileged': False,
            'ReadonlyRootfs': True,
            'Init': False,
            'PidMode': '',
            'IpcMode': 'private',
            'CgroupnsMode': 'private',
            'CapAdd': ['SETUID', 'SETGID'],
            'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true'],
            'NetworkMode': 'bridge',
            'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
            'Binds': None,
            'Devices': [],
            'DeviceRequests': None,
            'DeviceCgroupRules': None,
            'Runtime': 'runc',
            'Tmpfs': {'/work': WORK_TMPFS},
            'Memory': 128 * 1024**2,
            'MemorySwap': 128 * 1024**2,
            'PidsLimit': 16,
            'NanoCpus': 500_000_000,
        },
        'Mounts': [{'Type': 'volume', 'Name': VOLUME_NAME,
                    'Destination': '/control', 'RW': True}],
    }


class DeploymentContractTests(unittest.TestCase):
    def validate(self, facts, consumers=None, **kwargs):
        return validate_dedicated_deployment(
            facts, [CONTAINER_ID] if consumers is None else consumers,
            source_sha=kwargs.get('source_sha', SOURCE_SHA),
            volume_name=kwargs.get('volume_name', VOLUME_NAME),
            volume_inspect=kwargs.get('volume_inspect', volume_fixture()))

    def reject_host_values(self, field, values):
        for value in values:
            with self.subTest(field=field, value=value):
                facts = inspect_fixture()
                facts['HostConfig'][field] = value
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_complete_dedicated_facts_produce_exact_manifest_without_mutation(self):
        facts = inspect_fixture()
        before = copy.deepcopy(facts)
        consumers = [CONTAINER_ID]
        result = self.validate(facts, consumers)
        self.assertEqual(result, {
            'schema_version': 1, 'container_id': CONTAINER_ID,
            'source_sha': SOURCE_SHA, 'volume_name': VOLUME_NAME,
            'pid_mode': 'private', 'restart_policy': 'unless-stopped',
            'exclusive_volume': True,
        })
        self.assertEqual(facts, before)
        self.assertEqual(consumers, [CONTAINER_ID])
        self.assertNotIn('execution_allowed', result)

    def test_all_documented_root_user_spellings_are_accepted(self):
        for user in ('', '0', '0:0'):
            with self.subTest(user=user):
                facts = inspect_fixture()
                facts['Config']['User'] = user
                self.validate(facts)

    def test_container_id_requires_exact_lowercase_sha256_shape(self):
        for ident in ('a' * 63, 'a' * 65, 'A' * 64, '../container', '', None, True):
            with self.subTest(ident=ident):
                facts = inspect_fixture()
                facts['Id'] = ident
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_operator_source_sha_and_volume_name_are_strict(self):
        for key, values in {
            'source_sha': ('b' * 39, 'b' * 41, 'B' * 40, 'main', None, True),
            'volume_name': ('', '../state', '/control', 'x' * 129, None, True),
        }.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    with self.assertRaises(ContractError):
                        self.validate(inspect_fixture(), **{key: value})

    def test_wrong_purpose_or_source_label_rejected(self):
        for key, value in (('c3.purpose', 'existing-research-container'),
                           ('c3.source-sha', 'c' * 40)):
            with self.subTest(label=key):
                facts = inspect_fixture()
                facts['Config']['Labels'][key] = value
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_hostname_and_nonroot_user_mismatch_rejected(self):
        for key, values in {
            'Hostname': ('custom-hostname', CONTAINER_ID, '', None),
            'User': ('10001', '10002:10002', 'root', '0:10002', None, 0),
        }.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    facts = inspect_fixture()
                    facts['Config'][key] = value
                    with self.assertRaises(ContractError):
                        self.validate(facts)

    def test_only_the_literal_pid1_launcher_is_accepted(self):
        for entrypoint in (None, [], ['/bin/sh', '-c'],
                           ['python', '/opt/service/launcher.py'],
                           ['python', '-I', '-B', '/tmp/launcher.py'],
                           'python -I -B /opt/service/launcher.py'):
            with self.subTest(entrypoint=entrypoint):
                facts = inspect_fixture()
                facts['Config']['Entrypoint'] = entrypoint
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_launcher_command_override_is_rejected(self):
        for cmd in (['poll'], ['sh', '-c', 'sleep infinity'], '', False, 0):
            with self.subTest(cmd=cmd):
                facts = inspect_fixture()
                facts['Config']['Cmd'] = cmd
                with self.assertRaises(ContractError):
                    self.validate(facts)
        facts = inspect_fixture()
        facts['Config']['Cmd'] = []
        self.validate(facts)

    def test_init_wrapper_cannot_replace_launcher_as_pid1(self):
        self.reject_host_values('Init', (True, None, 0, 'false'))

    def test_missing_required_facts_are_rejected(self):
        paths = [('Id',), ('Config',), ('HostConfig',), ('Mounts',)]
        paths += [('Config', key) for key in inspect_fixture()['Config']]
        paths += [('Config', 'Labels', key) for key in inspect_fixture()['Config']['Labels']]
        paths += [('HostConfig', key) for key in inspect_fixture()['HostConfig']
                  if key != 'RestartPolicy']
        paths += [('HostConfig', 'RestartPolicy'),
                  ('HostConfig', 'RestartPolicy', 'Name')]
        for path in paths:
            with self.subTest(path=path):
                facts = inspect_fixture()
                parent = facts
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_malformed_inspect_and_nested_objects_are_rejected(self):
        for facts in (None, [], '', 1, True):
            with self.subTest(facts=facts):
                with self.assertRaises(ContractError):
                    self.validate(facts)
        for key in ('Config', 'HostConfig', 'Mounts'):
            for value in (None, True, '', []):
                with self.subTest(field=key, value=value):
                    facts = inspect_fixture()
                    facts[key] = value
                    with self.assertRaises(ContractError):
                        self.validate(facts)

    def test_privileged_and_writable_root_filesystem_rejected(self):
        self.reject_host_values('Privileged', (True, 0, None, 'false'))
        self.reject_host_values('ReadonlyRootfs', (False, 1, None, 'true'))

    def test_host_and_shared_pid_namespaces_rejected(self):
        self.reject_host_values('PidMode', ('host', 'container:' + 'c' * 64,
                                           'private', None, False))

    def test_host_and_shared_ipc_namespaces_rejected(self):
        self.reject_host_values('IpcMode', ('host', 'shareable', 'container:other',
                                           '', None, False))

    def test_host_and_unknown_cgroup_namespaces_rejected(self):
        self.reject_host_values('CgroupnsMode', ('host', '', 'unknown', None, False))

    def test_host_shared_and_unknown_network_modes_rejected(self):
        self.reject_host_values('NetworkMode', ('host', 'container:',
                                               'container:other', 'unknown-mode',
                                               '', 'none', 'default', None, False))

    def test_extra_or_missing_capabilities_rejected(self):
        self.reject_host_values('CapAdd', (None, [], ['SETUID'], ['ALL'],
                                           ['SETUID', 'SETGID', 'SYS_ADMIN'],
                                           ['SETUID', 'SETGID', 'KILL']))
        self.reject_host_values('CapDrop', (None, [], ['NET_RAW'], ['ALL', 'KILL']))

    def test_capability_fields_require_arrays(self):
        self.reject_host_values('CapAdd', ({'SETUID': True, 'SETGID': True},
                                           ('SETUID', 'SETGID')))
        self.reject_host_values('CapDrop', ({'ALL': True}, ('ALL',)))

    def test_missing_nnp_or_unconfined_security_options_rejected(self):
        self.reject_host_values('SecurityOpt', (None, [], ['no-new-privileges:false'],
                                                ['seccomp=unconfined'],
                                                ['no-new-privileges:true',
                                                 'apparmor=unconfined']))

    def test_wrong_restart_policy_rejected(self):
        for name in ('no', 'always', 'on-failure', '', None):
            with self.subTest(name=name):
                facts = inspect_fixture()
                facts['HostConfig']['RestartPolicy']['Name'] = name
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_host_binds_docker_socket_and_devices_rejected(self):
        self.reject_host_values('Binds', (['/tmp:/host'],
                                         ['/var/run/docker.sock:/var/run/docker.sock'],
                                         ['/home:/control:ro']))
        self.reject_host_values('Devices', ([{'PathOnHost': '/dev/sda',
                                             'PathInContainer': '/dev/sda',
                                             'CgroupPermissions': 'rwm'}],))

    def test_gpu_device_requests_rejected(self):
        self.reject_host_values('DeviceRequests', (
            [{'Driver': 'nvidia', 'Count': -1, 'DeviceIDs': None,
              'Capabilities': [['gpu']], 'Options': {}}],
            [{'Driver': '', 'Count': 1, 'Capabilities': [['gpu']]}],
        ))

    def test_custom_runtime_and_device_cgroup_rules_rejected(self):
        self.reject_host_values('Runtime', ('nvidia', 'runsc', '', None, False))
        self.reject_host_values('DeviceCgroupRules', (['a *:* rwm'], ['c 195:* rwm'],
                                                     '', {}, False))

    def test_worker_tmpfs_is_exact_and_cannot_shadow_control_or_application(self):
        self.reject_host_values('Tmpfs', (None, {}, [],
            {'/control': WORK_TMPFS}, {'/opt/service': WORK_TMPFS},
            {'/work': WORK_TMPFS, '/control': WORK_TMPFS},
            {'/work': WORK_TMPFS.replace('noexec,', '')},
            {'/work': WORK_TMPFS.replace('uid=10002', 'uid=0')},
            {'/work': WORK_TMPFS.replace('size=256m', 'size=8g')},
        ))

    def test_swap_must_be_disabled_with_same_strict_integer_memory_limit(self):
        self.reject_host_values('MemorySwap', (-1, 0, 1, None, True, '134217728',
                                               128 * 1024**2 + 1,
                                               float(128 * 1024**2)))

    def test_device_and_bind_fields_reject_unknown_empty_shapes(self):
        for field in ('Binds', 'Devices', 'DeviceRequests', 'DeviceCgroupRules'):
            self.reject_host_values(field, ('', {}, (), False, 0))

    def test_missing_unlimited_or_excessive_resource_limits_rejected(self):
        for field, ceiling in (('Memory', 1024**3), ('PidsLimit', 64),
                               ('NanoCpus', 10**9)):
            self.reject_host_values(field, (0, -1, ceiling + 1, None, '1',
                                             float('inf'), float('nan')))

    def test_resource_limits_require_integers_not_booleans_or_floats(self):
        for field in ('Memory', 'PidsLimit', 'NanoCpus'):
            self.reject_host_values(field, (True, False, 1.0, 0.5))

    def test_resource_upper_bounds_are_inclusive(self):
        facts = inspect_fixture()
        facts['HostConfig'].update(Memory=1024**3, MemorySwap=1024**3,
                                   PidsLimit=64, NanoCpus=10**9)
        self.validate(facts)

    def test_only_one_exact_control_volume_is_accepted(self):
        for mounts in ([], inspect_fixture()['Mounts'] * 2,
                       inspect_fixture()['Mounts'] + [
                           {'Type': 'bind', 'Source': '/var/run/docker.sock',
                            'Destination': '/var/run/docker.sock', 'RW': True}]):
            with self.subTest(mounts=mounts):
                facts = inspect_fixture()
                facts['Mounts'] = mounts
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_control_volume_identity_type_destination_and_access_are_exact(self):
        for key, values in {
            'Type': ('bind', 'tmpfs', None),
            'Name': ('another-volume', '', None),
            'Destination': ('/control/', '/work', '/', None),
            'RW': (False, 1, 'true', None),
        }.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    facts = inspect_fixture()
                    facts['Mounts'][0][key] = value
                    with self.assertRaises(ContractError):
                        self.validate(facts)
            with self.subTest(missing=key):
                facts = inspect_fixture()
                del facts['Mounts'][0][key]
                with self.assertRaises(ContractError):
                    self.validate(facts)

    def test_exclusive_volume_rejects_any_other_or_unknown_consumer(self):
        for consumers in ([], ['c' * 64], [CONTAINER_ID, 'c' * 64],
                          [CONTAINER_ID, CONTAINER_ID], [None], CONTAINER_ID,
                          (CONTAINER_ID,), {'container': CONTAINER_ID}, True):
            with self.subTest(consumers=consumers):
                with self.assertRaises(ContractError):
                    self.validate(inspect_fixture(), consumers)
        with self.assertRaises(ContractError):
            validate_dedicated_deployment(inspect_fixture(), None,
                                          source_sha=SOURCE_SHA, volume_name=VOLUME_NAME,
                                          volume_inspect=volume_fixture())

    def test_volume_inspect_requires_matching_local_volume_without_driver_options(self):
        for key, values in {
            'Name': ('another-volume', None),
            'Driver': ('nfs', 'third-party-plugin', None),
            'Scope': ('global', None),
            'Options': ({'type': 'none', 'o': 'bind', 'device': '/'},
                        {'type': 'nfs', 'device': 'server:/private'},
                        [], '', False),
        }.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    volume = volume_fixture()
                    volume[key] = value
                    with self.assertRaises(ContractError):
                        self.validate(inspect_fixture(), volume_inspect=volume)
            with self.subTest(missing=key):
                volume = volume_fixture()
                del volume[key]
                with self.assertRaises(ContractError):
                    self.validate(inspect_fixture(), volume_inspect=volume)

    def test_missing_or_malformed_volume_inspect_rejected(self):
        for volume in (None, [], '', True, {}):
            with self.subTest(volume=volume):
                with self.assertRaises(ContractError):
                    self.validate(inspect_fixture(), volume_inspect=volume)

    def test_empty_local_volume_options_are_accepted(self):
        volume = volume_fixture()
        volume['Options'] = {}
        self.validate(inspect_fixture(), volume_inspect=volume)


if __name__ == '__main__':
    unittest.main()
