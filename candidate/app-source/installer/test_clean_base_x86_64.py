"""clean_base renders the same recipe for ARM64 and switches only the
architecture-specific runtime pins (Node directory, Claude Code helper
package) when the image it renders into is x86_64."""
import ast
import importlib.util
import json
from pathlib import Path
import platform
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
BOOTSTRAP = (HERE.parent / 'app/harness/bootstrap.py').read_bytes()


def load(name, machine):
    with mock.patch.object(platform, 'machine', return_value=machine):
        spec = importlib.util.spec_from_file_location(name, HERE / 'clean_base.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


class CleanBaseArchTests(unittest.TestCase):
    def test_node_directory_follows_image_architecture(self):
        arm, mac, x86 = load('cb_arm', 'aarch64'), load('cb_mac', 'arm64'), load('cb_x86', 'x86_64')
        self.assertEqual(arm.NODE, '/usr/local/lib/decrypt-os-runtime/node-v24.19.0-linux-arm64')
        self.assertEqual(mac.NODE, arm.NODE)
        self.assertEqual(x86.NODE, '/usr/local/lib/decrypt-os-runtime/node-v24.19.0-linux-x64')
        self.assertEqual((arm.NODE_PLATFORM, x86.NODE_PLATFORM), ('linux-arm64', 'linux-x64'))

    def test_arm_bootstrap_transform_is_unchanged(self):
        arm = load('cb_arm2', 'aarch64')
        text = arm.adapt_bootstrap(BOOTSTRAP).decode()
        self.assertEqual(text, arm.adapt_bootstrap(BOOTSTRAP, 'aarch64').decode())
        self.assertIn('"@anthropic-ai/claude-code-linux-arm64"', text)
        self.assertNotIn('linux-x64', text)

    def test_x86_bootstrap_pins_the_x64_helper_package(self):
        x86 = load('cb_x86b', 'x86_64')
        text = x86.adapt_bootstrap(BOOTSTRAP).decode()
        ast.parse(text)
        self.assertNotIn('arm64', text)
        namespace = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '').startswith('CLAUDE_CODE_'):
                namespace[node.targets[0].id] = ast.literal_eval(node.value)
        self.assertEqual(namespace['CLAUDE_CODE_PACKAGE'], '@anthropic-ai/claude-code-linux-x64')
        self.assertEqual(namespace['CLAUDE_CODE_TARBALL'],
                         'https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.287.tgz')
        self.assertEqual(namespace['CLAUDE_CODE_INTEGRITY'],
                         'sha512-Q31ekN28viVjgfDuZKXUR5vxIBgUYoLGe+cRmjx/f01omqFDMi6IrgmcK5pWMnRbu/cjsTA/zO+bVES5oY3XUg==')
        self.assertIn('"cpu": ["x64"]', text)

    def test_runtime_manifest_architecture_value(self):
        manifest = json.loads((HERE.parent / 'app/harness/RUNTIME-MANIFEST.json').read_text())
        self.assertEqual(manifest['node']['architecture'], load('cb_arm3', 'aarch64').NODE_PLATFORM)


if __name__ == '__main__':
    unittest.main()
