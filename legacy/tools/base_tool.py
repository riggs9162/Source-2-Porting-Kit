"""
Base tool interface for the Source 2 Porting Kit.
All tools should inherit from this base class.
"""

import tkinter as tk
from tkinter import ttk
from abc import ABC, abstractmethod


class BaseTool(ABC):
    """Base class for all porting tools."""
    
    def __init__(self, config):
        self.config = config
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the display name of the tool."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Return a brief description of what the tool does."""
        pass
    
    @abstractmethod
    def create_tab(self, parent) -> ttk.Frame:
        """Create and return the GUI tab for this tool."""
        pass
    
    @property
    def dependencies(self) -> list:
        """Return a list of required Python packages for this tool."""
        return []
    
    @property
    def is_available(self) -> bool:
        """Check if all dependencies are available."""
        try:
            for dep in self.dependencies:
                __import__(dep)
            return True
        except ImportError:
            return False
    
    def get_unavailable_reason(self) -> str:
        """Return reason why tool is unavailable."""
        missing = []
        for dep in self.dependencies:
            try:
                __import__(dep)
            except ImportError:
                missing.append(dep)
        
        if missing:
            return f"Missing dependencies: {', '.join(missing)}"
        return "Tool is available"


class ToolRegistry:
    """Registry for managing available tools."""
    
    def __init__(self):
        self.tools = {}
    
    def register(self, tool_class):
        """Register a tool class."""
        self.tools[tool_class.__name__] = tool_class
    
    def get_available_tools(self, config):
        """Get all available tool instances."""
        return [tool_class(config) for tool_class in self.tools.values()]
    
    def get_tool(self, name, config):
        """Get a specific tool instance by name."""
        if name in self.tools:
            return self.tools[name](config)
        return None


# Global tool registry
tool_registry = ToolRegistry()


def register_tool(tool_class):
    """Decorator to register a tool."""
    tool_registry.register(tool_class)
    return tool_class
