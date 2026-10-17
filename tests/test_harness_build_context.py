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
