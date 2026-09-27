import logging
import sys
import os

def setup_logging(verbose: bool = False, debug: bool = False, log_file: str = "jirastats.log"):
    """
    Configures logging for the jirastats project.
    
    - verbose: If True, set console level to INFO.
    - debug: If True, set console level to DEBUG.
    - log_file: Path to a file where all logs (DEBUG and above) will be saved.
    """
    level = logging.WARNING
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO

    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # Always capture debug for file

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    if debug:
        console_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    else:
        # Cleaner format for standard/verbose output
        console_format = logging.Formatter('%(message)s')
        
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)

    # Force flush on console for real-time monitoring
    sys.stdout.flush()

    # File handler
    try:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_format)
        root_logger.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: Could not setup log file {log_file}: {e}", file=sys.stderr)

    # Suppress some noisy third-party loggers unless debugging
    if not debug:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
