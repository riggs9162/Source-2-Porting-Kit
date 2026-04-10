"""
File operation utility functions
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional


def copy_file(source: Path, destination: Path) -> bool:
    """
    Copy a file from source to destination
    
    Args:
        source: Source file path
        destination: Destination file path
        
    Returns:
        True if successful, False otherwise
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return True
    except (OSError, PermissionError, shutil.Error):
        return False


def move_file(source: Path, destination: Path) -> bool:
    """
    Move a file from source to destination
    
    Args:
        source: Source file path
        destination: Destination file path
        
    Returns:
        True if successful, False otherwise
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return True
    except (OSError, PermissionError, shutil.Error):
        return False


def get_files_by_extension(directory: Path, extension: str) -> List[Path]:
    """
    Get all files with a specific extension in a directory
    
    Args:
        directory: Directory to search
        extension: File extension (e.g., '.txt', '.vmt')
        
    Returns:
        List of file paths
    """
    if not directory.is_dir():
        return []
    
    if not extension.startswith('.'):
        extension = f'.{extension}'
    
    return list(directory.rglob(f'*{extension}'))


def get_file_size_mb(file_path: Path) -> float:
    """
    Get file size in megabytes
    
    Args:
        file_path: Path to file
        
    Returns:
        File size in MB
    """
    try:
        size_bytes = file_path.stat().st_size
        return size_bytes / (1024 * 1024)
    except (OSError, FileNotFoundError):
        return 0.0


def read_text_file(file_path: Path, encoding: str = 'utf-8') -> Optional[str]:
    """
    Read a text file
    
    Args:
        file_path: Path to file
        encoding: File encoding
        
    Returns:
        File contents or None if error
    """
    try:
        return file_path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError):
        return None


def write_text_file(file_path: Path, content: str, encoding: str = 'utf-8') -> bool:
    """
    Write content to a text file
    
    Args:
        file_path: Path to file
        content: Content to write
        encoding: File encoding
        
    Returns:
        True if successful, False otherwise
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding=encoding)
        return True
    except (OSError, PermissionError):
        return False
