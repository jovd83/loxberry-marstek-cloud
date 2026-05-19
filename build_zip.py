#!/usr/bin/env python3
import os
import re
import shutil
import zipfile
import configparser
from pathlib import Path

def main():
    # 1. Parse plugin.cfg for name and version using configparser
    plugin_cfg = Path("plugin.cfg")
    if not plugin_cfg.exists():
        print("Error: plugin.cfg not found in the current directory.")
        return

    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(plugin_cfg, encoding="utf-8")
    except Exception as e:
        print(f"Error reading plugin.cfg: {e}")
        return
        
    if "PLUGIN" not in config:
        print("Error: [PLUGIN] section not found in plugin.cfg.")
        return
        
    plugin_name = config["PLUGIN"].get("NAME", "").strip()
    plugin_version = config["PLUGIN"].get("VERSION", "").strip()
    
    if not plugin_name or not plugin_version:
        print("Error: Could not extract PLUGIN.NAME or PLUGIN.VERSION from plugin.cfg.")
        return
        
    zip_filename = f"{plugin_name}-{plugin_version}.zip"
    print(f"Packaging plugin '{plugin_name}' version '{plugin_version}'...")
    print(f"Creating archive: {zip_filename}")
    
    # 2. Files/directories to exclude
    exclude_patterns = [
        r"^\.git",
        r"^\.gitignore",
        r"^\.idea",
        r"^\.vscode",
        r"^build_zip\.py$",
        rf"^{plugin_name}-.*\.zip$",
        r"^jovd83-.*\.zip$"
    ]
    
    def should_exclude(path_str):
        path_str = path_str.replace("\\", "/")
        for pattern in exclude_patterns:
            if re.search(pattern, path_str):
                return True
        return False

    # 3. Build the zip file
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zip_file:
        count = 0
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]
            
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.relpath(filepath, ".")
                
                if should_exclude(rel_path):
                    continue
                
                archive_path = os.path.join(plugin_name, rel_path)
                zip_file.write(rel_path, archive_path)
                count += 1
                
        print(f"Successfully packaged {count} files into {zip_filename}!")

    # 4. Automatically copy to loxberry-integrator sandbox if it exists
    # Sibling check: c:\projects\Loxberry_plugins\Marstek-cloud -> c:\projects\skills\loxberry-integrator\sandbox
    integrator_sandbox = Path("../../skills/loxberry-integrator/sandbox")
    if integrator_sandbox.exists() and integrator_sandbox.is_dir():
        dest = integrator_sandbox / zip_filename
        try:
            shutil.copy2(zip_filename, dest)
            print(f"Copied package to sandbox folder: {dest}")
        except Exception as e:
            print(f"Warning: Could not copy to sandbox folder: {e}")
    else:
        print("Note: Sibling loxberry-integrator sandbox folder not found; package created locally only.")

if __name__ == "__main__":
    main()
