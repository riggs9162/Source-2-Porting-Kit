# Source 2 Porting Kit

A comprehensive toolkit for porting assets from Valve's Source 2 engine to Source 1, featuring a modern GUI interface and powerful automation tools.

![Half-Life VR](https://img.shields.io/badge/Half--Life-VR-orange)
![Python](https://img.shields.io/badge/Python-3.13+-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

## 🎯 Overview

The Source 2 Porting Kit simplifies the complex process of converting Source 2 assets back to Source 1. Whether you're working with textures, models, materials, or audio files, this toolkit provides automated workflows and professional-grade tools to streamline your porting projects.

## ✨ Features

### 🖼️ **Texture & Material Processing**
- **Texture Conversion**: Convert PNG/TGA images to VTF format with customizable settings
- **VMT Generation**: Automatically generate Source 1 material files from templates
- **PBR Baking**: Convert Source 2 PBR materials to Source 1 compatible shaders
- **Batch Processing**: Handle multiple textures simultaneously
- **Quality Control**: Preview and adjust textures before conversion

### 🎨 **Advanced Image Tools**
- **AO Baking**: Generate ambient occlusion maps
- **Brightness to Alpha**: Convert brightness values to alpha channels
- **Color Transparency**: Make specific colors transparent with tolerance control
- **Metal Transparency**: Create metallic surface effects
- **Subtexture Extraction**: Extract regions from larger texture atlases

### 🎵 **Audio Processing**
- **Loop Sound Converter**: Convert audio files to Source 1 loop formats
- **Quad to Stereo**: Convert surround sound to stereo
- **Format Support**: Handle multiple audio formats with automatic conversion

### 🏗️ **Model & Animation Tools**
- **QC Generation**: Automatically generate QC files for model compilation
- **Bone Backport**: Convert Source 2 bone structures to Source 1
- **SMD Processing**: Handle Source Model Data files with proper scaling
- **Batch Model Processing**: Convert multiple models efficiently

### 🔧 **Utility Tools**
- **Search & Replace**: Bulk text operations across multiple files
- **File Management**: Organize and rename files according to Source 1 conventions
- **Soundscape Processing**: Convert Source 2 soundscapes to Source 1 format
- **Configuration Management**: Save and load project settings

### 🎮 **Integration Features**
- **Discord Rich Presence**: Show your current work status
- **Drag & Drop Support**: Intuitive file handling
- **Real-time Preview**: See changes as you work
- **Progress Tracking**: Monitor long-running operations

## 🚀 Quick Start

### Option 1: Use the Executable (Recommended)
1. Download the latest `Source 2 Porting Kit.exe` from the releases
2. Run the executable - no installation required!
3. All dependencies are bundled, works on any Windows machine

### Option 2: Run from Source
1. **Install Python 3.13+**
2. **Clone the repository**:
   ```bash
   git clone https://github.com/riggs9162/Source-2-Porting-Kit.git
   cd Source-2-Porting-Kit
   ```
3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Run the application**:
   ```bash
   python porter.py
   ```

## 🔧 Building the Executable

To create your own executable:

### Windows (Easy)
```batch
build_exe.bat
```

### Cross-Platform
```bash
python build.py
```

The executable will be created in the `dist/` folder.

## 📋 Requirements

### For Executable Users
- Windows 10 or later
- No additional software required

### For Python Users
- Python 3.13 or later
- Dependencies listed in `requirements.txt`
- Optional: Source 2 Viewer for asset extraction

### Recommended Tools
- **Source 2 Viewer**: For extracting assets from Source 2 games
- **Blender**: For model editing and SMD export (with Source Tools addon)
- **Crowbar**: For compiling Source 1 models
- **VTFEdit**: For texture editing and preview

## 🎯 Workflow Guide

### 1. Asset Extraction
- Use Source 2 Viewer to extract models and textures from Source 2 games
- Export models as GLTF format
- Extract textures as PNG/TGA files

### 2. Model Processing
- Import GLTF files into Blender
- Scale models appropriately (typically 0.025 to 1.0 scale factor)
- Export as SMD files using Source Tools addon
- Use the QC Generation tool to create compilation files

### 3. Texture Conversion
- Load textures into the appropriate tools
- Convert to VTF format with desired settings
- Generate VMT files for materials
- Preview results before finalizing

### 4. Final Integration
- Compile models using Crowbar with generated QC files
- Test assets in your Source 1 environment
- Fine-tune materials and properties as needed

## 🛠️ Tool Categories

### Image Processing
- **AO Baker**: Generate ambient occlusion maps
- **Brightness to Alpha**: Convert brightness to transparency
- **Color Transparency**: Make colors transparent
- **Fake PBR Baker**: Convert PBR to Source 1 materials
- **Metal Transparency**: Create metallic effects
- **Subtexture Extraction**: Extract texture regions

### Audio Processing
- **Loop Sound Converter**: Convert to Source 1 audio loops
- **Quad to Stereo**: Downmix surround sound

### File Management
- **Search & Replace**: Bulk text operations
- **VMT Generator**: Create material files
- **VMT Duplicator**: Duplicate and modify materials
- **Soundscape Searcher**: Find and convert soundscapes

### Material Conversion
- **VMAT to VMT**: Convert Source 2 materials
- **Textures → VTF/VMT**: Comprehensive texture conversion

### Model Processing
- **Bone Backport**: Convert bone structures
- **QC Generation**: Create model compilation files
- **QC/SMD Prefix**: Batch rename operations

## 📁 Project Structure

```
Source-2-Porting-Kit/
├── porter.py              # Main application
├── porter.spec            # PyInstaller build configuration
├── requirements.txt       # Python dependencies
├── hlvr.ico              # Application icon
├── tools/                # Tool modules
│   ├── base_tool.py      # Base tool framework
│   ├── utils.py          # Utility functions
│   └── [tool_modules].py # Individual tools
├── VTFLibWrapper/        # VTF file support
└── old/                  # Legacy tools (archived)
```

## 🤝 Contributing

Contributions are welcome! Here's how to get started:

1. **Fork the repository**
2. **Create a feature branch**: `git checkout -b feature-name`
3. **Make your changes**
4. **Test thoroughly**
5. **Submit a pull request**

### Development Guidelines
- Follow existing code style and patterns
- Add tools to the `tools/` directory
- Use the `BaseTool` class for new tools
- Update documentation for new features

## 📝 License

This project is licensed under the [MIT License](LICENSE).

## 🆘 Support

- **Issues**: [GitHub Issues](https://github.com/riggs9162/Source-2-Porting-Kit/issues)
- **Documentation**: Check the tool-specific help within the application
- **Community**: Discord Rich Presence shows your progress to others

## 🙏 Acknowledgments

- Valve Software for the Source engine
- VTFLib developers for texture format support
- The Source modding community for continued innovation
- Contributors and testers who help improve the toolkit

---

**Made with ❤️ for the Source modding community**