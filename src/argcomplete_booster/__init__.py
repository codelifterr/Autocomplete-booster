"""Fast tab completion for argparse + argcomplete CLIs with slow imports.

This module is imported on every completion request, so it must only import
from the standard library modules that the interpreter has already loaded
(``os``, ``sys``). Everything else is imported lazily.
"""

__version__ = "0.1.0"

from ._boost import boost, static_completer

__all__ = ["boost", "static_completer", "__version__"]
