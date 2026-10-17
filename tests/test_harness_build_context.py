"""Every static harness dependency of the shipped image must be allowlisted."""
import ast
from pathlib import Path
import unittest

class BuildContextTests(unittest.TestCase):
    def test_runtime_imports_are_in_build_context(self):
        root=Path(__file__).resolve().parents[1]
        entries=(root/'tools/research_service/Dockerfile.dockerignore').read_text().splitlines()
        shipped={line[1:] for line in entries if line.startswith('!') and line.endswith('.py')}
        self.assertIn('research_harness/manager_github.py',shipped)
        for path in shipped:
            tree=ast.parse((root/path).read_text())
            for node in ast.walk(tree):
                if not isinstance(node,ast.ImportFrom) or not node.module:continue
                module=node.module
                if node.level and path.startswith('research_harness/'):
                    module='research_harness.'+module
                if module.startswith('research_harness.'):
                    target=module.replace('.','/')+'.py'
                    self.assertIn(target,shipped,(path,target))

    def test_pinned_codex_install_keeps_official_package_layout(self):
        root=Path(__file__).resolve().parents[1]
        dockerfile=(root/'tools/research_service/Dockerfile').read_text()
        self.assertIn('@openai/codex@0.160.0',dockerfile)
        self.assertIn('native_root="$(dirname "$(dirname "$native")")"',dockerfile)
        self.assertIn('test -f "$native_root/codex-package.json"',dockerfile)
        self.assertIn('test -x "$native_root/bin/codex-code-mode-host"',dockerfile)
        self.assertIn('cp -a "$native_root/." /opt/codex/',dockerfile)
        self.assertNotIn('install -m 0755 "$native"',dockerfile)
        self.assertLess(dockerfile.index('rm /opt/codex/bin/codex'),
                        dockerfile.index('cp -a "$native_root/."'))
