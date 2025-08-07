#!/usr/bin/env python3
"""
Build script for Source 2 Porting Kit executable using PyInstaller.
This script provides cross-platform building with advanced options.
"""

import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path

def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import PyInstaller
        print(f"✓ PyInstaller {PyInstaller.__version__} found")
    except ImportError:
        print("✗ PyInstaller not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✓ PyInstaller installed")

def install_requirements():
    """Install requirements from requirements.txt."""
    requirements_file = Path("requirements.txt")
    if requirements_file.exists():
        print("Installing requirements...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(requirements_file)])
        print("✓ Requirements installed")
    else:
        print("⚠ requirements.txt not found, skipping dependency installation")

def clean_build():
    """Clean previous build artifacts."""
    build_dirs = ["build", "dist", "__pycache__"]
    for dir_name in build_dirs:
        if os.path.exists(dir_name):
            print(f"Cleaning {dir_name}...")
            shutil.rmtree(dir_name)
    print("✓ Build directories cleaned")

def build_executable(debug=False, onefile=True, console=False):
    """Build the executable using PyInstaller."""
    cmd = [sys.executable, "-m", "PyInstaller"]
    
    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("porter.spec")
        
    if not console:
        cmd.append("--windowed")
        
    if debug:
        cmd.append("--debug=all")
    
    # Add icon if available
    icon_path = Path("hlvr.ico")
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
    
    # Specify additional options for onefile mode
    if onefile:
        cmd.extend([
            "--name", "Source 2 Porting Kit",
            "--add-data", "tools;tools",
            "--add-data", "VTFLibWrapper;VTFLibWrapper",
            "--hidden-import", "tools",
            "--hidden-import", "VTFLibWrapper",
            "--hidden-import", "PIL",
            "--hidden-import", "tkinterdnd2",
            "--hidden-import", "discordrp",
            "--hidden-import", "pydub",
            "--collect-all", "discordrp",
            "porter.py"
        ])
        
        # Add config.json if it exists
        config_path = Path("config.json")
        if config_path.exists():
            cmd.extend(["--add-data", "config.json;."])
    
    print(f"Building executable with command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Build completed successfully!")
        
        # Show output location
        if onefile:
            exe_path = Path("dist") / "Source 2 Porting Kit.exe"
        else:
            exe_path = Path("dist") / "Source 2 Porting Kit" / "Source 2 Porting Kit.exe"
            
        if exe_path.exists():
            print(f"Executable created: {exe_path}")
            print(f"  Size: {exe_path.stat().st_size / (1024*1024):.1f} MB")
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"Build failed with error code {e.returncode}")
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
        return False

def main():
    """Main build function."""
    parser = argparse.ArgumentParser(description="Build Source 2 Porting Kit executable")
    parser.add_argument("--debug", action="store_true", help="Build with debug information")
    parser.add_argument("--console", action="store_true", help="Show console window")
    parser.add_argument("--onedir", action="store_true", help="Create one-directory bundle instead of one-file")
    parser.add_argument("--no-clean", action="store_true", help="Don't clean build directories")
    parser.add_argument("--no-deps", action="store_true", help="Don't install dependencies")
    
    args = parser.parse_args()
    
    print("Source 2 Porting Kit - Build Script")
    print("=" * 40)
    
    # Change to script directory
    script_dir = Path(__file__).parent.absolute()
    os.chdir(script_dir)
    print(f"Working directory: {script_dir}")
    
    # Check dependencies
    if not args.no_deps:
        check_dependencies()
        install_requirements()
    
    # Clean build
    if not args.no_clean:
        clean_build()
    
    # Build executable
    onefile = not args.onedir
    success = build_executable(debug=args.debug, onefile=onefile, console=args.console)
    
    if success:
        print("\nBuild completed successfully!")
        print("\nNext steps:")
        print("1. Test the executable to ensure it works correctly")
        print("2. The executable is located in the 'dist' folder")
        print("3. You can distribute this folder/file to users")
        if not onefile:
            print("\nNote: When distributing a one-directory bundle, distribute the entire folder.")
    else:
        print("\nBuild failed. Check the error messages above.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
