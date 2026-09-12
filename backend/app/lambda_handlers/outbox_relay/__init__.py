"""用途別の入口を公開し、既存AWSのhandlerパスを維持する。"""

from .handler import assessment_handler
from .handler import embedding_handler as handler

__all__ = ["assessment_handler", "handler"]
