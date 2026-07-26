"""
General helper utility functions
"""

import os
import sys
from pathlib import Path
from typing import Optional


def get_app_dir() -> Path:
    """Get the application directory"""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        return Path(sys.executable).parent
    else:
        # Running as script
        return Path(__file__).parent.parent.parent


def get_config_dir() -> Path:
    """Get the configuration directory"""
    config_dir = get_app_dir() / "config"
    config_dir.mkdir(exist_ok=True)
    return config_dir


def validate_path(path: str) -> Optional[Path]:
    """
    Validate that a path exists
    
    Args:
        path: Path string to validate
        
    Returns:
        Path object if valid, None otherwise
    """
    try:
        p = Path(path)
        if p.exists():
            return p
    except (ValueError, OSError):
        pass
    return None


def ensure_dir(path: Path) -> bool:
    """
    Ensure a directory exists, create if it doesn't
    
    Args:
        path: Directory path
        
    Returns:
        True if directory exists or was created successfully
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except (OSError, PermissionError):
        return False


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing invalid characters
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename
