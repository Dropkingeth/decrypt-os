"""Cross-layer executable-to-package contract, no pacman or native execution."""
import ast
from pathlib import Path
import unittest

BASE=Path(__file__).resolve().parent

class DesktopRuntimeDependencyTests(unittest.TestCase):
    def test_required_notification_and_browser_commands_have_explicit_base_packages(self):
        tree=ast.parse((BASE.parent/'app-source/installer/clean_base.py').read_text())
        dependencies=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
            and any(isinstance(target,ast.Name) and target.id=='DEPENDENCIES' for target in node.targets))
        packages=[line.strip() for line in (BASE/'desktop-packages.txt').read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(len(packages),len(set(packages)))
        for command,package in {'notify-send':'libnotify','chromium':'chromium'}.items():
            self.assertIn(command,dependencies)
            self.assertIn(package,packages,command+' has no explicit base package provider')

if __name__=='__main__':unittest.main()
