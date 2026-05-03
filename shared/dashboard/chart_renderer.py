"""
Module: chart_renderer
Purpose: Idempotent annotated chart renderer for the dashboard.
Location: /opt/tickles/shared/dashboard/chart_renderer.py

This module takes a signal interpretation and its associated media item,
loads the original chart image, and overlays LLM-detected levels and
annotations using matplotlib. The result is saved as an SVG for high-quality
scaling in the dashboard.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from PIL import Image

logger = logging.getLogger(__name__)

# Constants
CHART_DIR = Path("/opt/tickles/opticals/charts")
DEFAULT_PROMPT_VERSION = "v1"

def _cache_key(interp_id: int, prompt_version: str, sr_hash: str) -> str:
    """Generate a stable filename for the cached SVG."""
    return f"{interp_id}_{prompt_version}_{sr_hash}.svg"

def _sr_hash(levels: Dict[str, Any], tags: Dict[str, Any]) -> str:
    """Generate a hash of the levels and tags to detect changes."""
    payload = {
        "levels": levels,
        "tags": tags
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return h[:8]

async def render_annotated_chart(interp_row: Dict[str, Any]) -> Path:
    """
    Idempotent chart renderer.
    
    Args:
        interp_row: Dictionary containing signal_interpretation data,
                    including joined media_item and news_item fields.
                    
    Returns:
        Path to the rendered SVG file.
    """
    try:
        interp_id = interp_row["id"]
        levels = interp_row.get("llm_levels") or {}
        if isinstance(levels, str):
            levels = json.loads(levels)
            
        tags = interp_row.get("pattern_tags") or {}
        if isinstance(tags, str):
            tags = json.loads(tags)
            
        prompt_version = interp_row.get("prompt_version") or DEFAULT_PROMPT_VERSION
        
        sr_h = _sr_hash(levels, tags)
        fname = _cache_key(interp_id, prompt_version, sr_h)
        out_path = CHART_DIR / fname
        
        if out_path.exists():
            return out_path
            
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        
        local_path = interp_row.get("local_path")
        if not local_path or not os.path.exists(local_path):
            logger.warning(f"Original chart image not found for interp {interp_id}: {local_path}")
            # Return a placeholder or raise? For now, let's raise to be handled by caller
            raise FileNotFoundError(f"Original image missing: {local_path}")

        # Load image to get dimensions
        img = mpimg.imread(local_path)
        height, width = img.shape[:2]
        
        # Create figure with matching aspect ratio
        dpi = 100
        fig, ax = plt.subplots(figsize=(width/dpi, height/dpi), dpi=dpi)
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        
        ax.imshow(img)
        ax.axis('off')
        
        # Draw levels if they look like numbers
        # Note: LLM might return strings like "50000" or "$50,000"
        def _to_float(val: Any) -> Optional[float]:
            if val is None: return None
            try:
                if isinstance(val, str):
                    val = val.replace("$", "").replace(",", "")
                return float(val)
            except (ValueError, TypeError):
                return None

        # We don't know the Y-axis mapping from price to pixels easily without
        # OCR or metadata. However, if the LLM gave us levels, we can at least
        # list them or try to find them if we had the candle data.
        # For now, we will overlay the text annotations in a clear box.
        
        annotation_text = []
        if levels:
            annotation_text.append("Detected Levels:")
            for k, v in levels.items():
                if v:
                    annotation_text.append(f"  {k}: {v}")
        
        if tags:
            if isinstance(tags, list):
                annotation_text.append(f"Patterns: {', '.join(tags)}")
            elif isinstance(tags, dict):
                annotation_text.append("Patterns:")
                for k, v in tags.items():
                    annotation_text.append(f"  {k}: {v}")

        if annotation_text:
            textstr = "\n".join(annotation_text)
            props = dict(boxstyle='round', facecolor='wheat', alpha=0.8)
            ax.text(0.02, 0.98, textstr, transform=ax.transAxes, fontsize=10,
                    verticalalignment='top', bbox=props)

        # Save as SVG
        fig.savefig(out_path, format="svg", bbox_inches="tight", pad_inches=0)
        plt.close(fig)
        
        return out_path
        
    except Exception as e:
        logger.error(f"Failed to render annotated chart for interp {interp_row.get('id')}: {e}")
        raise
