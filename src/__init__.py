import logging

# Library-style default: until setup_logging() runs, nothing from `src` reaches the console.
logging.getLogger(__name__).addHandler(logging.NullHandler())
