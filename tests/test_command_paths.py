import ast
from pathlib import Path


def test_privileged_runtime_commands_use_absolute_paths() -> None:
    root = Path(__file__).parents[1]
    files = [
        root / "deploy/install_batocera.py",
        root / "src/control_freak/app.py",
        root / "src/control_freak/audio.py",
        root / "src/control_freak/runtime.py",
        root / "src/control_freak/web.py",
    ]
    commands = {"tailscale", "batocera-audio", "pactl", "batocera-services"}
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                assert not (isinstance(first, ast.Constant) and first.value in commands), f"bare command in {path}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "shutil"
                    and node.func.attr == "which"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in commands
                ), f"PATH lookup in {path}"
