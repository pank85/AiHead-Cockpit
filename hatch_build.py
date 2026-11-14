"""Hatchling build hook for the platform-specific Rust cockpit."""

from pathlib import Path
import shutil
import subprocess

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        # The sdist uses the allowlist in pyproject.toml. Never force-include a
        # directory: that bypasses Hatchling's selection and exclusion rules.
        if self.target_name != "wheel":
            return
        manifest = root / "rust" / "cockpit" / "Cargo.toml"
        target = root / "build" / "cargo"
        subprocess.run(["cargo", "build", "--locked", "--release", "--manifest-path", str(manifest),
                        "--target-dir", str(target)], check=True, cwd=str(root))
        binary = target / "release" / "aihead-cockpit"
        if not binary.is_file():
            raise RuntimeError(f"Cargo did not produce native cockpit binary: {binary}")
        build_data["pure_python"] = False
        build_data["infer_tag"] = True
        if version == "editable":
            # Editable wheels import the source tree, not wheel package resources.
            destination = root / "src" / "aiheaddev" / "_native" / "aihead-cockpit"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, destination)
            destination.chmod(destination.stat().st_mode | 0o111)
        else:
            build_data.setdefault("force_include", {})[str(binary)] = "aiheaddev/_native/aihead-cockpit"
