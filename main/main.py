"""
Source 2 Porting Kit - Main Entry Point
A tool for porting Source 2 content
"""

import sys
import traceback
import logging
import threading
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon
from PySide6.QtCore import qInstallMessageHandler, QtMsgType
from app.ui.main_window import MainWindow
from app.ui.styling import StyleManager
from app.core.settings import Settings


# Setup logging
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "app.log"

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def qt_message_handler(mode, context, message):
    """Handle Qt messages and log them"""
    if mode == QtMsgType.QtDebugMsg:
        logger.debug(f"Qt: {message}")
    elif mode == QtMsgType.QtInfoMsg:
        logger.info(f"Qt: {message}")
    elif mode == QtMsgType.QtWarningMsg:
        logger.warning(f"Qt: {message}")
    elif mode == QtMsgType.QtCriticalMsg:
        logger.error(f"Qt: {message}")
    elif mode == QtMsgType.QtFatalMsg:
        logger.critical(f"Qt: {message}")


def handle_exception(exc_type, exc_value, exc_traceback):
    """Global exception handler"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    
    # Format the error
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    tb_text = ''.join(tb_lines)
    
    # Try to show error dialog if Qt is available
    try:
        app = QApplication.instance()
        if app:
            error_msg = QMessageBox()
            error_msg.setIcon(QMessageBox.Critical)
            error_msg.setWindowTitle("Application Error")
            error_msg.setText(f"An unexpected error occurred:\n\n{exc_type.__name__}: {exc_value}")
            error_msg.setDetailedText(tb_text)
            error_msg.setStandardButtons(QMessageBox.Ok)
            error_msg.exec()
    except:
        pass
    
    # Always print to console
    print("\n" + "="*80, file=sys.stderr)
    print("FATAL ERROR - Application Crashed", file=sys.stderr)
    print("="*80, file=sys.stderr)
    print(tb_text, file=sys.stderr)
    print("="*80, file=sys.stderr)
    print(f"Error log saved to: {log_file}", file=sys.stderr)


def handle_threading_exception(args):
    """Handle exceptions in threads"""
    logger.critical(
        f"Uncaught exception in thread {args.thread.name}",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )
    
    # Format the error
    tb_lines = traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    tb_text = ''.join(tb_lines)
    
    # Print to console
    print("\n" + "="*80, file=sys.stderr)
    print(f"THREAD CRASH - {args.thread.name}", file=sys.stderr)
    print("="*80, file=sys.stderr)
    print(tb_text, file=sys.stderr)
    print("="*80, file=sys.stderr)
    print(f"Error log saved to: {log_file}", file=sys.stderr)


def main():
    """Main application entry point"""
    # Install global exception handlers
    sys.excepthook = handle_exception
    threading.excepthook = handle_threading_exception
    
    try:
        logger.info("="*80)
        logger.info("Starting Source 2 Porting Kit")
        logger.info("="*80)
        
        app = QApplication(sys.argv)
        app.setApplicationName("Source 2 Porting Kit")
        app.setOrganizationName("Source 2 Porting Kit")
        
        # Install Qt message handler
        qInstallMessageHandler(qt_message_handler)
        
        # Load settings
        logger.info("Loading settings...")
        settings = Settings()
        
        # Set application icon (if exists)
        icon_path = Path(__file__).parent / "app" / "resources" / "icon.ico"
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
            logger.info(f"Loaded application icon: {icon_path}")
        
        # Apply font
        logger.info("Applying font settings...")
        StyleManager.set_font(
            app, 
            settings.get('font_family', 'Segoe UI'),
            settings.get('font_size', 10)
        )
        
        # Apply theme
        theme = settings.get_theme()
        logger.info(f"Applying theme: {theme}")
        StyleManager.apply_theme(app, theme)
        
        # Create and show main window
        logger.info("Creating main window...")
        window = MainWindow(settings)
        window.show()
        logger.info("Application started successfully")
        
        exit_code = app.exec()
        logger.info(f"Application exiting with code {exit_code}")
        sys.exit(exit_code)
        
    except Exception as e:
        logger.critical(f"Failed to start application: {e}", exc_info=True)
        
        # Show error dialog
        try:
            error_msg = QMessageBox()
            error_msg.setIcon(QMessageBox.Critical)
            error_msg.setWindowTitle("Startup Error")
            error_msg.setText(f"Failed to start the application:\n\n{str(e)}")
            error_msg.setDetailedText(traceback.format_exc())
            error_msg.setStandardButtons(QMessageBox.Ok)
            error_msg.exec()
        except:
            print(f"FATAL ERROR: {e}", file=sys.stderr)
            traceback.print_exc()
        
        sys.exit(1)


if __name__ == "__main__":
    main()
