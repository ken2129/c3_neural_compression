"""Static deployment contract; Docker itself is not available in this runner."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]/'tools/research_service'

class PublicationRecoveryImageTests(unittest.TestCase):
    def test_no_model_install_or_supervisor_entrypoint(self):
        image=(ROOT/'Dockerfile.publication-recovery').read_text()
        self.assertIn('gh ca-certificates',image)
        for forbidden in ('npm install','@openai/codex','launcher.py','SETUID','SETGID'):
            self.assertNotIn(forbidden,image)
        self.assertIn('USER 10001:10001',image)
        self.assertIn('ENTRYPOINT ["python","-I","-B","/opt/service/recover_publication.py"]',image)
        self.assertIn('CMD ["--once"]',image)

    def test_one_shot_existing_named_volume_no_host_authority(self):
        compose=(ROOT/'compose.publication-recovery.yaml').read_text()
        for expected in ('restart: "no"','cap_drop: [ALL]','read_only: true',
                         'external: true','command: ["--once"]','pids_limit: 64',
                         'mem_limit: 1g','cpus: 1','user: "10001:10001"'):
            self.assertIn(expected,compose)
        for forbidden in ('privileged:','cap_add:','pid: host','/var/run/docker.sock','devices:'):
            self.assertNotIn(forbidden,compose)

    def test_build_context_explicit_entrypoint_and_recovery_module(self):
        ignore=(ROOT/'Dockerfile.publication-recovery.dockerignore').read_text()
        self.assertTrue(ignore.startswith('**\n'))
        self.assertIn('!research_harness/publication_recovery.py',ignore)
        self.assertIn('!tools/research_service/recover_publication.py',ignore)
