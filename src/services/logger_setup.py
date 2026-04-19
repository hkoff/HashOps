# src/services/logger_setup.py — Centralized log system configuration

import logging
import colorama
from colorama import Fore, Style

from src.config import DEBUG_MODE

# Initialize colorama for Windows support
colorama.init(autoreset=True)

def setup_logger(name: str = "hCASH") -> logging.Logger:
    """
    Configures and returns a logger instance with colored formatting.
    Log level is controlled by DEBUG_MODE in config.py.
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        # Keep the global level at DEBUG so all handlers (including SSE) 
        # correctly receive all events.
        logger.setLevel(logging.DEBUG)
        
        # Format: [TIME] LEVEL - MESSAGE
        formatter = logging.Formatter(
            fmt='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # Console Handler — Always in DEBUG for the terminal
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        logger.addHandler(console_handler)
        
    return logger

# Shared global instance
logger = setup_logger()
logger.info(f"{Fore.CYAN}hCASH Logger initialized{Style.RESET_ALL}")

